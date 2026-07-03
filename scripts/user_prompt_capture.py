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

ROOT = Path(os.environ.get("LLM_WIKI_ROOT", str(Path(__file__).resolve().parent.parent))).resolve()
STATE_ROOT = Path(
    os.environ.get("LLM_WIKI_STATE_ROOT", str(Path(__file__).resolve().parent.parent.parent / "LLM-wiki-state"))
).resolve()
DAILY_DIR = ROOT / "memory" / "daily"

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
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _compute_slug_from_cwd(cwd: str) -> str:
    """Resolve project slug using the existing 5-step collision logic.

    Reuses session_start_project_state._compute_slug so prompts are
    tagged with the SAME slug that state.md uses — no drift.
    """
    projects_dir = ROOT / "wiki" / "projects"
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
        state_file = STATE_ROOT / "memory-state" / "state.json"
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
        state_file = STATE_ROOT / "memory-state" / "state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        if state_file.exists():
            state = json.loads(state_file.read_text(encoding="utf-8"))
        else:
            state = {}
        key = f"{slug}::{prompt_hash}"
        state.setdefault("prompt_capture_dedupe", {})[key] = datetime.now().isoformat(
            timespec="seconds"
        )
        # Keep dedupe map bounded — keep only last 100 entries.
        if len(state["prompt_capture_dedupe"]) > 100:
            items = sorted(
                state["prompt_capture_dedupe"].items(),
                key=lambda kv: kv[1],
                reverse=True,
            )[:100]
            state["prompt_capture_dedupe"] = dict(items)
        # Atomic write via tmp+replace.
        tmp = state_file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(state_file)
    except Exception:  # noqa: BLE001
        pass  # never fail the hook on dedupe-bookkeeping


def _append_prompt_tag(slug: str, session_id: str, preview: str) -> None:
    """Append a one-line breadcrumb to today's daily log."""
    try:
        DAILY_DIR.mkdir(parents=True, exist_ok=True)
        day = datetime.now().strftime("%Y-%m-%d")
        path = DAILY_DIR / f"{day}.md"
        if not path.exists():
            path.write_text(f"# Daily Session Memory — {day}\n", encoding="utf-8")
        ts = datetime.now().strftime("%H:%M:%S")
        line = (
            f"- `[{ts}] prompt | {session_id[:8]} | {slug}` "
            f"{preview[:MAX_PROMPT_PREVIEW]}\n"
        )
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
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
            if cwd_resolved == ROOT:
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
