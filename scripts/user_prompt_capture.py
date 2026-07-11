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
- Never fails the hook (exits 0 always) — hook failures break sessions.
- Only writes for sessions OUTSIDE the vault itself. Vault-internal
  sessions (where cwd = LLM_WIKI_ROOT) are typically maintenance and
  would create a feedback loop.

Input (Claude Code UserPromptSubmit hook JSON on stdin):
    {"session_id": "...", "prompt": "user text", "cwd": "..."}

Output: a JSON `{"continue": true}` on stdout (or empty — both work).
"""
from __future__ import annotations

import hashlib
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
try:
    from memory_state import ROOT as _MS_ROOT  # noqa: E402
    from memory_state import STATE_ROOT as _MS_STATE
    from memory_state import update_state
    ROOT = Path(os.environ.get("LLM_WIKI_ROOT", str(_MS_ROOT))).resolve()
    STATE_ROOT = Path(os.environ.get("LLM_WIKI_STATE_ROOT", str(_MS_STATE))).resolve()
except Exception:  # noqa: BLE001
    # memory_state unavailable — resolve paths but skip state writes (no
    # unlocked fallback writer that could clobber concurrent locked writes).
    ROOT = Path(os.environ.get("LLM_WIKI_ROOT", str(Path(__file__).resolve().parent.parent))).resolve()
    STATE_ROOT = Path(
        os.environ.get("LLM_WIKI_STATE_ROOT", str(ROOT))
    ).resolve()

    def update_state(mutator):  # type: ignore[misc]
        """No-op stub — safe skip when memory_state is unavailable."""
        pass

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


def _read_hook_input() -> dict:
    """Parse Claude Code hook JSON from stdin. Tolerant of empty stdin."""
    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001
        return {}
    if not raw or not raw.strip():
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


def _rate_limited(slug: str, prompt_hash: str) -> bool:
    """True if this (slug, prompt) was logged in the last RATE_LIMIT_SECONDS."""
    try:
        state_file = STATE_ROOT / "run" / "state.json"
        if not state_file.exists():
            return False
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    key = f"{slug}::{prompt_hash}"
    last = state.get("prompt_capture_dedupe", {}).get(key)
    if not last:
        return False
    try:
        age = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
        return age < RATE_LIMIT_SECONDS
    except (ValueError, TypeError):
        return False


def _record_dedupe(slug: str, prompt_hash: str) -> None:
    """Record this (slug, prompt) in the dedupe map. Best-effort."""
    try:
        key = f"{slug}::{prompt_hash}"
        now = datetime.now().isoformat(timespec="seconds")

        def _mutate(state: dict) -> None:
            state.setdefault("prompt_capture_dedupe", {})[key] = now
            if len(state["prompt_capture_dedupe"]) > 100:
                items = sorted(
                    state["prompt_capture_dedupe"].items(),
                    key=lambda kv: kv[1],
                    reverse=True,
                )[:100]
                state["prompt_capture_dedupe"] = dict(items)

        update_state(_mutate)
    except Exception:  # noqa: BLE001
        pass  # never fail the hook on dedupe-bookkeeping


def _append_prompt_tag(slug: str, session_id: str, preview: str) -> None:
    """Append a one-line breadcrumb to today's daily log."""
    try:
        from daily_log_append import append_daily

        ts = datetime.now().strftime("%H:%M:%S")
        safe = redact_secrets(preview)[:MAX_PROMPT_PREVIEW]
        block = (
            f"- `[{ts}] prompt | {session_id[:8]} | {slug}` "
            f"{safe}"
        )
        append_daily(slug, session_id, block)
    except Exception:  # noqa: BLE001
        pass  # never fail the hook on disk-write


def main() -> int:
    try:
        hook = _read_hook_input()
        prompt = (hook.get("prompt") or "").strip()
        session_id = hook.get("session_id") or "unknown"
        cwd = hook.get("cwd") or os.getcwd()

        # Skip prompts that are too short to be meaningful.
        if len(prompt) < MIN_PROMPT_CHARS:
            return 0

        # Skip sessions inside the vault itself (maintenance loops).
        try:
            cwd_resolved = Path(cwd).resolve()
            if cwd_resolved.is_relative_to(ROOT):
                return 0
        except Exception:  # noqa: BLE001
            pass

        slug = _compute_slug_from_cwd(cwd)

        # Rate-limit by prompt content hash (so the same question
        # retried 5 times in a row only logs once per window).
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
        if _rate_limited(slug, prompt_hash):
            return 0

        _append_prompt_tag(slug, session_id, prompt)
        _record_dedupe(slug, prompt_hash)
    except Exception:  # noqa: BLE001
        # Last-resort: never break the user's session over a logging hook.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
