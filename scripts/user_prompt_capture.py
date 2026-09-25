"""UserPromptSubmit hook — lightweight prompt tagger.

Appends a single non-LLM breadcrumb line per user prompt to today's
daily log, so the episodic record shows WHAT was asked (not just when
sessions ended). Pairs with PostToolUse capture to give compile_memory
the input signal it needs to decide what's worth lifting.

Design constraints (Phase 1):
- NON-LLM. No SDK calls. ms-fast.
- Rate-limited: at most one line per (slug, prompt_hash) per 30s window
  to avoid log explosion during rapid re-prompts.
- Skips empty/whitespace prompts.
- Never fails the hook once it runs (a failure is recorded, exit 0); a module
  that cannot import exits non-zero and the adapter records the loss.
- Writes for sessions anywhere, the vault included: the memory's own processes
  are filtered by the adapter's reentry marker, which is what the old
  "vault-internal sessions are maintenance" rule stood in for (2026-09-24).

Input (Claude Code UserPromptSubmit hook JSON on stdin):
    {"session_id": "...", "prompt": "user text", "cwd": "..."}

Output: a JSON `{"continue": true}` on stdout (or empty — both work).
"""
from __future__ import annotations

import io
import json
import os
import sys
from datetime import datetime
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
from memory_state import STATE_ROOT as _MS_STATE  # noqa: E402
from memory_state import spawn_detached, update_state  # noqa: E402

ROOT = Path(os.environ.get("LLM_WIKI_ROOT", str(_MS_ROOT))).resolve()
STATE_ROOT = Path(os.environ.get("LLM_WIKI_STATE_ROOT", str(_MS_STATE))).resolve()

from capture_operation import claim_operation, complete_operation  # noqa: E402

try:
    from capture_diagnostics import record_capture_failure  # noqa: E402
except Exception:  # noqa: BLE001
    def record_capture_failure(kind, reason, **fields):  # type: ignore[misc]
        """No-op stub — diagnostics must never break the capture hook."""

from event_envelope import build_event_envelope  # noqa: E402
from memory_state import HOOK_STATE_LOCK_TIMEOUT  # noqa: E402
from secret_redact import redact_secrets  # noqa: E402

DAILY_DIR = ROOT / "knowledge" / "daily"

# Rate-limit window per (slug, prompt-hash). Prevents log explosion
# during rapid re-prompts or autocomplete-style submissions.
RATE_LIMIT_SECONDS = 30

# Skip prompts shorter than this — they are usually autocomplete noise
# or accidental Enter presses, not real user intent.
MIN_PROMPT_CHARS = 5

# How many chars of the prompt to log. Long prompts (paste of files,
# stack traces) shouldn't blow up the daily log.
MAX_PROMPT_PREVIEW = 140
FLUSH_MESSAGE_INTERVAL = 20
ADVISORY_REFRESH_INTERVAL = 10


def _read_stdin() -> str:
    try:
        return sys.stdin.read()
    except Exception:  # noqa: BLE001
        return ""


def _read_hook_input() -> dict:
    """Parse Claude Code hook JSON from stdin. Tolerant of empty stdin."""
    raw = _read_stdin()
    if not raw.strip():
        return {}
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return result if isinstance(result, dict) else {}


def _compute_slug_from_cwd(cwd: str) -> str:
    """Resolve project slug using the existing 5-step collision logic.

    Reuses session_start_project_state._compute_slug so prompts are
    tagged with the SAME slug that state.md uses — no drift.
    """
    projects_dir = ROOT / "knowledge" / "projects"
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from session_start_project_state import _compute_slug  # type: ignore

        return _compute_slug(Path(cwd).resolve(), projects_dir)
    except Exception:  # noqa: BLE001
        # Fall back to parent-dir name lowercased — same as the
        # first step of the full slug algorithm.
        try:
            return Path(cwd).resolve().name.lower().replace(" ", "-")
        except Exception:  # noqa: BLE001
            return "unknown"


def _claim_prompt_operation(
    slug: str, prompt_hash: str, *, source_event_id: str | None = None
) -> str | None:
    key = f"{slug}::{prompt_hash}"
    return claim_operation(
        lambda mutate: update_state(mutate, lock_timeout=HOOK_STATE_LOCK_TIMEOUT),
        namespace="prompt_capture_dedupe",
        key=key,
        prefix="user-prompt",
        source_event_id=source_event_id,
        rate_limit_seconds=RATE_LIMIT_SECONDS,
        max_entries=100,
        now=datetime.now(),
    )


def _complete_prompt_operation(
    slug: str, prompt_hash: str, operation_id: str
) -> None:
    key = f"{slug}::{prompt_hash}"
    complete_operation(
        lambda mutate: update_state(mutate, lock_timeout=HOOK_STATE_LOCK_TIMEOUT),
        namespace="prompt_capture_dedupe",
        key=key,
        operation_id=operation_id,
        now=datetime.now(),
    )


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

    try:
        update_state(_mutate, lock_timeout=HOOK_STATE_LOCK_TIMEOUT)
        return count
    except Exception:  # noqa: BLE001
        return 0


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


def _append_prompt_tag(
    slug: str, session_id: str, preview: str, operation_id: str | None = None
) -> bool:
    """Append a one-line breadcrumb to today's daily log."""
    try:
        from daily_log_append import (
            BREADCRUMB_APPEND_BUDGET_SECONDS,
            append_daily,
            append_deadline,
        )

        ts = datetime.now().strftime("%H:%M:%S")
        safe = redact_secrets(preview)[:MAX_PROMPT_PREVIEW]
        block = (
            f"- `[{ts}] prompt | {session_id[:8]} | {slug}` "
            f"{safe}"
        )
        # A deadline inside the host's: without one the append retried until the
        # host cancelled the hook, and the lost breadcrumb left no reason.
        append_daily(
            slug,
            session_id,
            block,
            operation_id=operation_id,
            deadline=append_deadline(BREADCRUMB_APPEND_BUDGET_SECONDS),
        )
        return True
    except Exception as error:  # noqa: BLE001
        record_capture_failure(
            "user_prompt_append",
            f"{type(error).__name__}: {error}",
            error=error,
            slug=slug,
            session_id=session_id,
        )
        return False


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _hook_cwd(hook: dict) -> str:
    return str(hook.get("cwd") or os.getcwd())


def _hook_session(hook: dict) -> str:
    return str(hook.get("session_id") or "unknown")


def _should_skip(prompt: str) -> bool:
    """Too short to be a prompt worth a line.

    It also skipped every prompt inside the vault, as "maintenance loops"; the
    adapter's reentry marker has named the memory's own processes exactly since
    2026-09-17, and the vault rule only hid the owner's own sessions there. See
    `docs/research/2026-09-24-a-long-session-in-the-vault-is-captured.md`.
    """
    return len(prompt) < MIN_PROMPT_CHARS


def _prompt_envelope(hook: dict, safe_prompt: str, slug: str):
    """Build the canonical event envelope for one captured prompt."""
    source_cwd = hook.get("cwd")
    source_session = hook.get("session_id")
    return build_event_envelope(
        event_type="user_prompt",
        payload={"prompt": safe_prompt},
        agent=_optional_string(hook.get("agent")),
        session=str(source_session) if source_session is not None else None,
        project=slug if source_cwd else None,
        worktree=str(source_cwd) if source_cwd else None,
        severity=_optional_string(hook.get("severity")),
        parent_event_id=_optional_string(hook.get("parent_event_id")),
        source_event_id=_optional_string(hook.get("event_id")),
    )


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


def _record_prompt(hook: dict, prompt: str) -> None:
    """Claim, append, and complete one prompt capture."""
    session_id = _hook_session(hook)
    slug = _compute_slug_from_cwd(_hook_cwd(hook))
    envelope = _prompt_envelope(hook, redact_secrets(prompt), slug)
    _maybe_periodic_work(hook, session_id, _increment_prompt_count(session_id, slug))

    # Rate-limit by the redacted payload hash so capture state cannot
    # become a side channel for source secrets.
    prompt_hash = envelope.content_hash[:12]
    operation_id = _claim_prompt_operation(
        slug,
        prompt_hash,
        source_event_id=envelope.source_event_id,
    )
    if operation_id is None:
        return
    appended = _append_prompt_tag(
        slug,
        session_id,
        envelope.payload["prompt"],
        operation_id=operation_id,
    )
    if appended:
        _complete_prompt_operation(slug, prompt_hash, operation_id)


def main() -> int:
    try:
        hook = _read_hook_input()
        prompt = str(hook.get("prompt") or "").strip()
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
