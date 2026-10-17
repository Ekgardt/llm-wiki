"""UserPromptSubmit compatibility entrypoint for durable, non-LLM capture.

The common adapter preserves the complete redacted occurrence before advisory
or project follow-ups. Delivery to the original day's journal is recoverable.
Empty prompts are ignored; separate meaningful occurrences are never suppressed
by their text, size, or frequency. Failures are recorded without blocking the host.
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

# Force UTF-8 stdout (Windows console default is cp1251 — breaks emoji
# and non-ASCII prompts).
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
# A missing `memory_state` used to be replaced by no-op stand-ins, so the hook ran
# and silently wrote nothing; now the import fails, the process exits non-zero,
# and the adapter records the lost capture (`_record_failed_delegate`).
from memory_state import ROOT as _MS_ROOT  # noqa: E402
from memory_state import spawn_detached, update_state  # noqa: E402

ROOT = Path(os.environ.get("LLM_WIKI_ROOT", str(_MS_ROOT))).resolve()

from capture_diagnostics import hook_object, record_capture_failure  # noqa: E402
from memory_state import HOOK_STATE_LOCK_TIMEOUT  # noqa: E402
from session_start_project_state import _compute_slug  # noqa: E402

# Legacy full-transcript checkpoint cadence; every prompt now has independent
# durable ingress. Basis unknown for this unchanged interval; pending Law 9 review.
FLUSH_MESSAGE_INTERVAL = 20
# Legacy advisory cadence, independent of capture acceptance. Basis unknown for
# this unchanged interval; pending Law 9 review, not proof of an optimal budget.
ADVISORY_REFRESH_INTERVAL = 10


def _read_hook_input() -> dict:
    """Parse Claude Code hook JSON from stdin. Tolerant of empty stdin."""
    # Read errors belong to main's existing recorded-failure boundary; they
    # must not be turned into an ordinary empty host invocation.
    return hook_object(sys.stdin.read(), "prompt_input")


def _compute_slug_from_cwd(cwd: str) -> str:
    """Resolve project slug using the existing 5-step collision logic.

    Reuses session_start_project_state._compute_slug so prompts are
    tagged with the SAME slug that state.md uses — no drift.
    """
    try:
        return _compute_slug(Path(cwd).resolve(), ROOT / "knowledge" / "projects")
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


def _increment_prompt_count(session_id: str, slug: str) -> int:
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
    update_state(_mutate, lock_timeout=HOOK_STATE_LOCK_TIMEOUT)
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
    spawn_detached([
        sys.executable,
        str(ROOT / "scripts" / "integration_adapter.py"),
        "--source", canonical_agent(str(hook.get("agent") or "claude")),
        "--running-capture", json.dumps(payload, ensure_ascii=False),
    ])


def _build_advisory_refresh() -> str:
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


def _should_skip(prompt: str) -> bool:
    """Only empty input is skipped.

    It also skipped every prompt inside the vault, as "maintenance loops"; the
    adapter's reentry marker has named the memory's own processes exactly since
    2026-09-17, and the vault rule only hid the owner's own sessions there. See
    `docs/research/2026-09-24-a-long-session-in-the-vault-is-captured.md`.
    """
    return not prompt.strip()




def _maybe_periodic_work(hook: dict, session_id: str, prompt_count: int) -> None:
    """Advisory refresh and periodic flush ride on the prompt counter."""
    if not prompt_count:
        return
    _periodic_work(hook, session_id, prompt_count)


def _periodic_work(hook: dict, session_id: str, prompt_count: int) -> None:
    if prompt_count % ADVISORY_REFRESH_INTERVAL == 0:
        _write_advisory_output(_build_advisory_refresh())
    if prompt_count % FLUSH_MESSAGE_INTERVAL == 0:
        _spawn_periodic_flush(hook, session_id)


def after_prompt_capture(hook: dict, slug: str | None) -> None:
    """Run advisory work only after the common ingress accepted the event."""
    session_id = _hook_session(hook)
    slug = slug or _compute_slug_from_cwd(_hook_cwd(hook))
    _maybe_periodic_work(hook, session_id, _increment_prompt_count(session_id, slug))


def _record_prompt(hook: dict, prompt: str) -> None:
    """Direct hook invocations use the same durable publisher as host adapters."""
    from integration_adapter import ingest_event, normalize_occurrence_event

    envelope = normalize_occurrence_event(
        str(hook.get("agent") or "claude"), "user_prompt", {**hook, "prompt": prompt},
    )
    ingest_event(envelope)


def main() -> int:
    try:
        hook = _read_hook_input()
        prompt = str(hook.get("prompt") or "")
        if _should_skip(prompt):
            return 0
        _record_prompt(hook, prompt)
    except Exception as error:  # noqa: BLE001
        # Last-resort: never break the user's session over a logging hook,
        # but never lose the capture silently either.
        record_capture_failure(
            "user_prompt_hook", f"{type(error).__name__}: {error}", error=error
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
