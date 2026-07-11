"""Flush one session event into knowledge/daily/YYYY-MM-DD.md.

Run as a detached background process by PreCompact / SessionEnd hooks.

Responsibilities:
1. Read the transcript at `--transcript` (JSONL Claude Code transcript).
2. Ask the unified llm_client (auto-detected backend: OpenCode / Codex /
   Claude CLI / OpenAI / Ollama) to classify + summarize the session
   into one of three tiers (Phase 0.5 upgrade):
     - FLUSH_MAJOR: decisions/lessons/non-obvious commands worth compiling
     - FLUSH_MINOR: only gotchas/debug-notes/open-questions — save but no auto-compile
     - FLUSH_OK:    pure status/progress chatter — skip entirely
3. For MAJOR/MINOR: append the structured summary to today's daily log
   with an `[HH:MM:SS] event | session_id` header block and a `Tier:`
   metadata line. For OK: do not append anything.
4. Dedupe: skip if the same (session_id, event) was flushed in the last 60s.
5. If local time >= MEMORY_COMPILE_AFTER_HOUR (default 18) AND tier is
   MAJOR AND today's daily log changed since last compile: spawn
   compile via `maybe_compile` (PID-locked). (MINOR no longer triggers
   compile — this prevents the compile pipeline from churning on
   sessions that contain only minor gotchas.)

The 3-tier scale replaces the previous binary FLUSH_OK/no-FLUSH_OK.
Empirically the old threshold was too aggressive (12 consecutive
empty flushes recorded in state.json as of 2026-04-23): the LLM
returned FLUSH_OK for any session that lacked a clean "decisions"
section, even when useful gotchas or commands were present.

State lives in $LLM_WIKI_STATE_ROOT/run/state.json (default:
$LLM_WIKI_ROOT/run/state.json — inside the vault, gitignored) so git
doesn't track runtime churn.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from maybe_compile import spawn_compile_if_idle  # noqa: E402
from memory_state import (  # noqa: E402
    ROOT,
    file_hash,
    load_state,
    update_state,
)
from secret_redact import redact_secrets  # noqa: E402

DAILY_DIR = ROOT / "knowledge" / "daily"
DEDUPE_WINDOW_SECONDS = 60
MAX_TRANSCRIPT_CHARS = 60_000

# Tier sentinels — replace the legacy single FLUSH_OK. The classifier
# is asked to emit exactly one of these as the FIRST line of its
# response, followed (for MAJOR/MINOR) by the structured summary.
TIERS = ("FLUSH_MAJOR", "FLUSH_MINOR", "FLUSH_OK")

# Legacy sentinel still recognized for backward compat with any
# pre-Phase-0.5 summaries that may already be in flight or persisted.
LEGACY_SENTINELS = ("FLUSH_OK", "(no durable content)", "NO_DURABLE_CONTENT")


def _classify_response(raw: str) -> tuple[str, str]:
    """Split the LLM response into (tier, body).

    Accepts the new protocol (first line is FLUSH_MAJOR / FLUSH_MINOR /
    FLUSH_OK) and the legacy protocol (single FLUSH_OK token anywhere).

    Returns:
        ("FLUSH_MAJOR" | "FLUSH_MINOR" | "FLUSH_OK", remaining_text)

    The remaining_text is the structured summary for MAJOR/MINOR tiers
    and is empty for OK.
    """
    if not raw or not raw.strip():
        return "ok", ""
    stripped = raw.strip()
    first_line_raw = stripped.splitlines()[0].strip()
    first_line = first_line_raw.upper().rstrip(".")
    # Strip surrounding backticks the LLM may have added around the token.
    while first_line.startswith("`") and first_line.endswith("`") and len(first_line) > 1:
        first_line = first_line[1:-1]
    # New protocol: first line is exactly one of the tiers
    if first_line in TIERS:
        # Body = everything after the first line, cleaned.
        body = stripped[len(first_line_raw) :].strip(" \n\t*`")
        return first_line.lower().replace("flush_", ""), body
    # Legacy protocol: FLUSH_OK as a single-word line anywhere
    norm = stripped.strip(" .\n\t*`").upper()
    for sentinel in LEGACY_SENTINELS:
        if norm == sentinel.upper():
            return "ok", ""
    for ln in stripped.splitlines():
        if ln.strip().upper() in LEGACY_SENTINELS:
            return "ok", ""
    # Failure sentinel from summarizer crash
    if stripped.startswith("(summary failed"):
        return "ok", ""
    # Detect compile-plan JSON masquerading as flush response
    if stripped.startswith('{"operations"') or stripped.startswith('{"audit"'):
        return "ok", ""
    # No recognized sentinel: treat as MINOR (preserve content, don't
    # auto-compile — operator can manually trigger compile if needed).
    # This is a defensive default: better to save potentially-useful
    # content as MINOR than to lose it as OK.
    return "minor", stripped


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--event", required=True, choices=["session-end", "pre-compact"])
    p.add_argument("--session-id", default="unknown")
    p.add_argument("--transcript", default="")
    p.add_argument("--trigger", default="")
    return p.parse_args()


def _transcript_path_allowed(path: Path) -> bool:
    """Only allow transcript paths from known agent session directories.

    `transcript_path` arrives from hook JSON (untrusted input). A broad
    allowlist (e.g. all of ``$HOME``) would let a crafted payload point
    at ``~/.ssh/id_rsa`` and ship its contents to the LLM. Instead we
    restrict to the specific directories where Claude Code, Codex, and
    OpenCode store session transcripts, plus the system temp dir and the
    vault-local cache for testing.
    """
    import tempfile

    try:
        p = path.resolve()
    except OSError:
        return False

    allowed_prefixes: list[Path] = []
    # Claude Code transcripts
    home = Path.home()
    allowed_prefixes.append(home / ".claude")
    # Codex transcripts
    allowed_prefixes.append(home / ".codex")
    # OpenCode transcripts
    allowed_prefixes.append(home / ".config" / "opencode")
    # Vault-local temp (for testing)
    if ROOT.exists():
        allowed_prefixes.append(ROOT / "cache")
    # System temp (for Claude Code compacted transcripts)
    allowed_prefixes.append(Path(tempfile.gettempdir()))

    # Must also have a known transcript extension.
    if p.suffix not in (".jsonl", ".json", ".txt", ".log"):
        return False

    # Must be under one of the allowed directories.
    for prefix in allowed_prefixes:
        try:
            prefix_resolved = prefix.resolve()
        except OSError:
            continue
        try:
            p.relative_to(prefix_resolved)
            return True
        except ValueError:
            continue
    return False


def read_transcript_tail(path: Path, max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
    if not path.exists():
        return ""
    if not _transcript_path_allowed(path):
        return ""
    try:
        data = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    if len(data) > max_chars:
        data = data[-max_chars:]
    return data


def summarize_with_llm(transcript_excerpt: str, event: str) -> str:
    """Ask the LLM to classify + distill the transcript into a tier + body.

    Uses the unified llm_client (auto-detected backend — no separate API
    key required on this machine). Falls back gracefully if the LLM is
    unavailable (returns "" → caller treats as FLUSH_OK).
    """
    if not transcript_excerpt.strip():
        return ""
    transcript_excerpt = redact_secrets(transcript_excerpt)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from llm_client import call_llm
    except ImportError:
        return ""

    prompt = f"""You are classifying + distilling a Claude Code session transcript.

Event: {event}

=== STEP 1 — CLASSIFY ===
First, decide the tier of this session by scanning the transcript:

- FLUSH_MAJOR  — contains at least one of: a concrete DECISION with
  rationale, a reusable LESSON/pattern, or a non-obvious COMMAND/snippet
  worth remembering across sessions.

- FLUSH_MINOR  — contains only one or more of: a debug GOTCHA
  (symptom→cause), an OPEN QUESTION worth returning to, or a single
  useful observation — but no decisions or lessons. Worth saving but
  not worth auto-compiling.

- FLUSH_OK     — pure status/progress chatter ("we did X", "started Y",
  "fixed Z" without explanation). Nothing a future session would benefit
  from. Empty transcripts and pure-navigation turn here too.

Be strict. Status updates are FLUSH_OK even if they mention real work —
the bar is "would a future session in this project benefit from knowing
this?". When in doubt, choose the lower tier.

=== STEP 2 — DISTILL (skip for FLUSH_OK) ===
For FLUSH_MAJOR and FLUSH_MINOR, produce a Markdown block with ONLY
these sections that apply (skip empty sections):

- **Decisions made** — concrete choices with reasons (MAJOR only).
- **Lessons / patterns** — reusable insights (MAJOR only).
- **Commands / snippets** — non-obvious invocations (any tier).
- **Gotchas / debugging** — symptom → cause → fix (any tier).
- **Open questions** — unresolved, worth returning to (any tier).

Be terse. Each bullet should fit on one line. Do NOT narrate what was
done — that is status, not memory.

=== OUTPUT FORMAT ===
Emit EXACTLY one of these tokens as the FIRST line of your response,
followed by a blank line, then (if MAJOR/MINOR) the distilled block:

For MAJOR:
FLUSH_MAJOR

<distilled markdown block>

For MINOR:
FLUSH_MINOR

<distilled markdown block>

For OK (no second line allowed):
FLUSH_OK

Do not add preamble, apologies, or trailing explanation. The first
non-blank line MUST be the tier token.

--- BEGIN TRANSCRIPT EXCERPT ---
{transcript_excerpt}
--- END TRANSCRIPT EXCERPT ---
"""

    system_prompt = (
        "You classify and distill Claude Code transcripts into a 3-tier "
        "memory scale. Your default bias is toward FLUSH_OK — most "
        "sessions are status chatter and should not pollute the daily "
        "log. You only emit FLUSH_MAJOR when you can point to a concrete "
        "decision or lesson in the transcript. No preamble, no apologies."
    )
    try:
        text = call_llm(prompt, system_prompt, max_tokens=1500)
    except Exception:
        return ""
    if not text:
        # No backend available (call_llm returned None) — enqueue for
        # deferred processing so the content isn't silently lost as
        # FLUSH_OK. Drained at the next active session via memory_queue.
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from memory_queue import enqueue
            enqueue("flush", {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "max_tokens": 1500,
                "enqueued_by": "flush_memory",
                "event": event,
            })
        except Exception:
            pass  # best-effort — never crash the hook
        return ""
    return text.strip()


def append_daily(day: str, block: str) -> Path:
    from daily_log_append import locked_append

    out = DAILY_DIR / f"{day}.md"
    locked_append(out, block)
    return out


def dedupe_key(session_id: str, event: str) -> str:
    return f"{session_id}::{event}"


def should_skip(state: dict, session_id: str, event: str) -> bool:
    last = state.get("flush_dedupe", {}).get(dedupe_key(session_id, event))
    if not last:
        return False
    return (time.time() - float(last)) < DEDUPE_WINDOW_SECONDS


def record_flush(state: dict, session_id: str, event: str) -> None:
    dedupe = state.setdefault("flush_dedupe", {})
    dedupe[dedupe_key(session_id, event)] = time.time()
    # Prune stale entries so the dict doesn't grow unbounded.
    cutoff = time.time() - DEDUPE_WINDOW_SECONDS * 4
    stale = [k for k, ts in dedupe.items() if float(ts) < cutoff]
    for k in stale:
        del dedupe[k]


def maybe_trigger_compile(state: dict, daily_path: Path, tier: str) -> None:
    """Spawn compile only for FLUSH_MAJOR content, after the hour cutoff.

    Always goes through `maybe_compile.spawn_compile_if_idle` so the PID
    lock is the single concurrency gate (hooks / wrappers / schedulers
    must not spawn `compile_memory.py` directly).
    """
    if tier != "major":
        return
    try:
        hour_cutoff = int(os.environ.get("MEMORY_COMPILE_AFTER_HOUR", "18"))
    except ValueError:
        hour_cutoff = 18
    if datetime.now().hour < hour_cutoff:
        return
    current_hash = file_hash(daily_path)
    compiled = state.get("compiled_daily_hashes", {}).get(daily_path.name)
    if compiled == current_hash:
        return

    # Cooldown: on a busy day every session-end after 18:00 mutates the
    # daily log (hash changes) and would otherwise re-spawn a compile
    # process each time. Rate-limit to one spawn per cooldown window.
    # Tune via MEMORY_COMPILE_COOLDOWN_SECONDS (default 900 = 15 min).
    # Set to 0 to disable cooldown entirely.
    try:
        cooldown_s = int(os.environ.get("MEMORY_COMPILE_COOLDOWN_SECONDS", "900"))
    except ValueError:
        cooldown_s = 900
    if cooldown_s > 0:
        last_spawned_raw = state.get("last_compile_spawned_at")
        if last_spawned_raw:
            try:
                last_spawned = datetime.fromisoformat(last_spawned_raw)
                elapsed = (datetime.now() - last_spawned).total_seconds()
                if elapsed < cooldown_s:
                    return
            except (ValueError, TypeError):
                pass

    spawned_at = datetime.now().isoformat(timespec="seconds")
    spawned, reason = spawn_compile_if_idle(force=False)
    if spawned:
        state["last_compile_spawned_at"] = spawned_at
    state["last_compile_spawned_trigger"] = "auto"
    state["last_compile_spawned_daily"] = daily_path.name
    state["last_compile_spawned_tier"] = tier
    state["last_compile_spawned_reason"] = reason
    state.setdefault("compile_triggers", []).append(
        {
            "at": spawned_at,
            "daily": daily_path.name,
            "trigger": "auto",
            "tier": tier,
            "spawned": spawned,
            "reason": reason,
        }
    )
    state["compile_triggers"] = state["compile_triggers"][-20:]


def main() -> int:
    args = parse_args()
    # Read-only peek for the dedupe short-circuit; the real write happens
    # inside update_state() below so we don't race with compile_memory.
    if should_skip(load_state(), args.session_id, args.event):
        return 0

    transcript = read_transcript_tail(Path(args.transcript)) if args.transcript else ""
    raw_summary = summarize_with_llm(transcript, args.event) if transcript else ""

    # 3-tier classification (Phase 0.5). Replaces binary FLUSH_OK check.
    tier, body = _classify_response(raw_summary)

    # 3b. Feedback capture — scan for correction/preference patterns.
    # If the user corrected the agent during this session, save as a
    # feedback candidate for later promotion. Non-blocking.
    if tier in ("major", "minor") and body:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from feedback_capture import capture_from_text
            capture_from_text(
                body,
                session_id=args.session_id,
                slug="unknown",
                trigger=args.event,
            )
        except Exception:
            pass

    # FLUSH_OK: nothing worth persisting. Still record dedupe so retries
    # don't hammer the SDK. Still consider auto-compile in case the day's
    # log already has prior MAJOR content worth compiling.
    if tier == "ok":
        def _mutate_noop(state: dict) -> None:
            record_flush(state, args.session_id, args.event)
            state.setdefault("flush_empty_count", 0)
            state["flush_empty_count"] = int(state.get("flush_empty_count", 0)) + 1
            state["last_flush_empty_at"] = datetime.now().isoformat(timespec="seconds")
            # Track tier distribution for observability.
            state.setdefault("flush_tier_counts", {})
            state["flush_tier_counts"]["ok"] = int(state["flush_tier_counts"].get("ok", 0)) + 1

        update_state(_mutate_noop)
        return 0

    # FLUSH_MAJOR or FLUSH_MINOR: append the structured body to today's
    # daily log with a Tier: marker so the compile pipeline and human
    # readers can see what kind of content this is.
    now = datetime.now()
    day = now.strftime("%Y-%m-%d")
    header = f"\n## [{now.strftime('%H:%M:%S')}] {args.event} | {args.session_id}\n"
    meta = (
        f"- Trigger: `{args.trigger}`\n"
        f"- Transcript: `{args.transcript}`\n"
        f"- Tier: `{tier}`\n"
    )
    body_block = body + "\n" if body else "(tier flagged but no structured body — manual review needed)\n"
    block = header + meta + "\n" + body_block
    # Mandatory redaction boundary: strip secrets before the block lands
    # in the durable daily log (mirrors post_tool_capture.py:66-72).
    block = redact_secrets(block)

    deferred_compiles: list[tuple[Path, str]] = []

    def _mutate(state: dict) -> None:
        if should_skip(state, args.session_id, args.event):
            return
        daily_path = append_daily(day, block)
        record_flush(state, args.session_id, args.event)
        state.setdefault("flush_tier_counts", {})
        state["flush_tier_counts"][tier] = int(state["flush_tier_counts"].get(tier, 0)) + 1
        if tier == "major":
            deferred_compiles.append((daily_path, tier))

    update_state(_mutate)

    for daily_path, flush_tier in deferred_compiles:
        def _trigger_and_persist(state: dict, _dp=daily_path, _ft=flush_tier) -> None:
            maybe_trigger_compile(state, _dp, _ft)
        update_state(_trigger_and_persist)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
