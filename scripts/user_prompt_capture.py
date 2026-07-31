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
    # memory_state unavailable — resolve paths but skip state writes (no
    # unlocked fallback writer that could clobber concurrent locked writes).
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


def _append_prompt_tag(
    slug: str,
    project_root: Path,
    session_id: str,
    preview: str,
) -> None:
    """Append a one-line breadcrumb to today's daily log."""
    from daily_log_append import append_daily

    ts = datetime.now().strftime("%H:%M:%S")
    safe = " ".join(redact_secrets(preview).split())[:MAX_PROMPT_PREVIEW]
    block = (
        f"- `[{ts}] prompt | {session_id[:8]} | {slug}` "
        f"project-root-json={json.dumps(str(project_root), ensure_ascii=False)} | {safe}"
    )
    append_daily(slug, session_id, block)


def _capture_prompt_once(
    slug: str,
    project_root: Path,
    session_id: str,
    prompt: str,
    prompt_hash: str,
) -> bool:
    """Atomically dedupe, append, then record a successful prompt capture."""
    key = f"{slug}::{prompt_hash}"
    now = datetime.now()
    appended = False

    def _mutate(state: dict) -> None:
        nonlocal appended
        dedupe = state.get("prompt_capture_dedupe")
        if not isinstance(dedupe, dict):
            dedupe = {}
            state["prompt_capture_dedupe"] = dedupe
        last = dedupe.get(key)
        if last:
            try:
                if (now - datetime.fromisoformat(str(last))).total_seconds() < RATE_LIMIT_SECONDS:
                    return
            except (ValueError, TypeError):
                pass
        _append_prompt_tag(slug, project_root, session_id, prompt)
        dedupe[key] = now.isoformat(timespec="seconds")
        if len(dedupe) > 100:
            state["prompt_capture_dedupe"] = dict(
                sorted(dedupe.items(), key=lambda item: item[1], reverse=True)[:100]
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
        prompt = (hook.get("prompt") or "").strip()
        session_id = hook.get("session_id") or "unknown"
        resolution = resolve_project_root(
            hook,
            env=os.environ,
            fallback_cwd=os.getcwd(),
        )
        project_root = resolution.root

        # Skip prompts that are too short to be meaningful.
        if len(prompt) < MIN_PROMPT_CHARS:
            return 0
        if project_root is None:
            return 0

        # Skip sessions inside the vault itself (maintenance loops).
        try:
            if project_root.is_relative_to(ROOT):
                return 0
        except Exception:  # noqa: BLE001
            pass

        slug = _compute_slug_from_cwd(str(project_root))
        if slug is None:
            return 0

        # Rate-limit by prompt content hash (so the same question
        # retried 5 times in a row only logs once per window).
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
        _capture_prompt_once(slug, project_root, session_id, prompt, prompt_hash)
    except Exception:  # noqa: BLE001
        # Last-resort: never break the user's session over a logging hook.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
