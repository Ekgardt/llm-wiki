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
4. Dedupe: skip if the same project/session/event occurrence was flushed in
   the last 60s.
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
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from maybe_compile import spawn_compile_if_idle  # noqa: E402
from memory_state import (  # noqa: E402
    ROOT,
    file_hash,
    load_state,
    trusted_compiled_daily_hashes,
    update_state,
)
from secret_redact import redact_secrets  # noqa: E402

DAILY_DIR = ROOT / "knowledge" / "daily"
DEDUPE_WINDOW_SECONDS = 60
MAX_TRANSCRIPT_CHARS = 60_000
STAGED_TRANSCRIPT_PREFIX = "llm-wiki-precompact-"
STAGED_TRANSCRIPT_SUFFIX = ".txt"
MAX_PROVENANCE_CHARS = 500
BODY_XML_TAG_RE = re.compile(r"</?(?:analysis|summary)>", re.IGNORECASE)
BODY_HEADING_PREFIX_RE = re.compile(r"^##\s+\[")
BODY_COMPACT_PREFIX_RE = re.compile(r"^\s*-\s*`\[")
DAILY_RECORD_COMPLETION_MARKER = "<!-- llm-wiki-record-complete -->"

# Tier sentinels — replace the legacy single FLUSH_OK. The classifier
# is asked to emit exactly one of these as the FIRST line of its
# response, followed (for MAJOR/MINOR) by the structured summary.
TIERS = ("FLUSH_MAJOR", "FLUSH_MINOR", "FLUSH_OK")

# Legacy sentinel still recognized for backward compat with any
# pre-Phase-0.5 summaries that may already be in flight or persisted.
LEGACY_SENTINELS = ("FLUSH_OK", "(no durable content)", "NO_DURABLE_CONTENT")


@dataclass(frozen=True)
class TranscriptReadResult:
    text: str
    successful: bool


@dataclass(frozen=True)
class FlushProcessStatus:
    code: int
    durable: bool
    project_slug: str
    project_root: str


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
        tier = first_line.lower().replace("flush_", "")
        return tier, body
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


def _occurred_datetime(value: str | None, fallback: datetime | None = None) -> datetime:
    raw = str(value or "").strip()
    if raw:
        try:
            return datetime.fromisoformat(
                f"{raw[:-1]}+00:00" if raw.endswith(("Z", "z")) else raw
            )
        except (TypeError, ValueError):
            pass
    return fallback or datetime.now()


def _resolve_project_identity(
    project_slug: str | None,
    project_root: str | None,
) -> tuple[str, Path] | None:
    explicit = str(project_slug or "").strip()
    raw_root = str(project_root or "").strip()
    try:
        from session_start_project_state import (
            _slug_identity_key,
            confirm_project_identity,
            resolve_project_root,
        )

        resolution = resolve_project_root(
            {"project_root": raw_root} if raw_root else {},
            env=os.environ,
        )
        resolved_root = resolution.root
        if resolved_root is None or not resolved_root.is_dir():
            return None

        confirmed = confirm_project_identity(
            resolved_root,
            ROOT / "knowledge" / "projects",
        )
        if confirmed is None:
            return None
        slug = confirmed[0]
        return (
            (slug, resolved_root)
            if not explicit or _slug_identity_key(explicit) == _slug_identity_key(slug)
            else None
        )
    except Exception:  # noqa: BLE001 - persistence must fail closed
        return None


def _resolve_project_slug(
    project_slug: str | None,
    project_root: str | None,
) -> str | None:
    identity = _resolve_project_identity(project_slug, project_root)
    return identity[0] if identity is not None else None


def _sanitize_provenance(value: object, default: str = "unknown") -> str:
    redacted = redact_secrets(str(value or default))
    one_line = " ".join(redacted.split()).replace("`", "'")
    return one_line[:MAX_PROVENANCE_CHARS] or default


def _neutralize_daily_record_headers(body: str) -> str:
    lines: list[str] = []
    for line in body.splitlines():
        normalized = BODY_XML_TAG_RE.sub("", line)
        marker = "#" if BODY_HEADING_PREFIX_RE.match(normalized) else ""
        if not marker and BODY_COMPACT_PREFIX_RE.match(normalized):
            marker = "-"
        if not marker and normalized == DAILY_RECORD_COMPLETION_MARKER:
            marker = "<"
        position = line.find(marker) if marker else -1
        lines.append(
            f"{line[:position]}\\{line[position:]}" if position >= 0 else line
        )
    return "\n".join(lines)


def render_flush_block(
    tier: str,
    body: str,
    *,
    event: str,
    session_id: str,
    trigger: str,
    project_slug: str,
    project_root: str,
    occurred_at: str,
    deferred: bool = False,
    idempotency_marker: str = "",
) -> tuple[str, str]:
    """Render and redact one classified daily block from source metadata."""
    occurred = _occurred_datetime(_sanitize_provenance(occurred_at, ""))
    event_name = _sanitize_provenance(event, "session-end")
    if deferred:
        event_name = _sanitize_provenance(f"deferred-{event_name}")
    event_name = event_name.replace("|", "/")
    source_session = _sanitize_provenance(session_id).replace("|", "/")
    source_trigger = _sanitize_provenance(trigger)
    raw_root = str(project_root or "").strip()
    try:
        source_root = (
            str(Path(raw_root).resolve())
            if Path(raw_root).is_absolute()
            else _sanitize_provenance(raw_root)
        )
    except (OSError, RuntimeError, ValueError):
        source_root = _sanitize_provenance(raw_root)
    source_slug = _sanitize_provenance(project_slug)
    source_tier = _sanitize_provenance(tier)
    header = (
        f"\n## [{occurred.strftime('%H:%M:%S')}] {event_name} | {source_session}\n"
    )
    meta = (
        f"- Trigger: `{source_trigger}`\n"
        f"- Project slug: `{source_slug}`\n"
        f"- Project root JSON: {json.dumps(source_root, ensure_ascii=False)}\n"
        f"- Tier: `{source_tier}`\n"
        f"- Source session: `{source_session}`\n"
    )
    safe_body = _neutralize_daily_record_headers(redact_secrets(body.strip()))
    safe_marker = _sanitize_provenance(idempotency_marker, "")
    marker = f"{safe_marker}\n" if safe_marker else ""
    block = (
        header
        + meta
        + "\n"
        + safe_body
        + "\n"
        + marker
        + DAILY_RECORD_COMPLETION_MARKER
        + "\n"
    )
    return occurred.strftime("%Y-%m-%d"), block


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--event", required=True, choices=["session-end", "pre-compact"])
    p.add_argument("--session-id", default="unknown")
    p.add_argument("--transcript", default="")
    p.add_argument("--transcript-stdin", action="store_true")
    p.add_argument("--delete-transcript", action="store_true")
    p.add_argument("--trigger", default="")
    p.add_argument("--project-slug", default="")
    p.add_argument("--project-root", default="")
    p.add_argument("--occurred-at", default="")
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


def read_stream_tail(stream, max_chars: int, chunk_size: int = 8_192) -> str:
    """Read a bounded text tail without first allocating the whole input."""
    if max_chars <= 0 or chunk_size <= 0:
        return ""
    tail = ""
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            return tail
        tail = (tail + chunk)[-max_chars:]


def _staged_transcript_allowed(path: Path) -> bool:
    """Allow deletion only for regular files in our exact temp namespace."""
    if (
        not path.name.startswith(STAGED_TRANSCRIPT_PREFIX)
        or not path.name.endswith(STAGED_TRANSCRIPT_SUFFIX)
    ):
        return False
    try:
        if path.parent.resolve() != Path(tempfile.gettempdir()).resolve():
            return False
        metadata = path.lstat()
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    return (
        stat.S_ISREG(metadata.st_mode)
        and not path.is_symlink()
        and not (reparse_flag and file_attributes & reparse_flag)
    )


def _read_transcript_tail_result(
    path: Path,
    max_chars: int = MAX_TRANSCRIPT_CHARS,
    *,
    staged: bool = False,
) -> TranscriptReadResult:
    try:
        allowed = (
            _staged_transcript_allowed(path)
            if staged
            else _transcript_path_allowed(path)
        )
        if not allowed:
            return TranscriptReadResult("", False)
        with path.open(encoding="utf-8", errors="ignore") as stream:
            return TranscriptReadResult(
                read_stream_tail(stream, max_chars),
                True,
            )
    except OSError:
        return TranscriptReadResult("", False)


def read_transcript_tail(
    path: Path,
    max_chars: int = MAX_TRANSCRIPT_CHARS,
    *,
    delete_after: bool = False,
) -> str:
    result = _read_transcript_tail_result(
        path,
        max_chars,
        staged=delete_after,
    )
    if delete_after and result.successful:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return result.text


def _build_flush_queue_payload(
    transcript_excerpt: str,
    event: str,
    *,
    session_id: str = "unknown",
    trigger: str = "",
    project_slug: str = "",
    project_root: str = "",
    occurred_at: str = "",
) -> dict[str, object] | None:
    """Build one redacted immediate/deferred classification request."""
    if not transcript_excerpt.strip():
        return None
    transcript_excerpt = redact_secrets(transcript_excerpt)

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
    return {
        "prompt": prompt,
        "system_prompt": system_prompt,
        "max_tokens": 1500,
        "enqueued_by": "flush_memory",
        "event": event,
        "session_id": session_id,
        "trigger": trigger,
        "project_slug": project_slug,
        "project_root": project_root,
        "occurred_at": occurred_at,
    }


def _enqueue_flush_payload(payload: dict[str, object]) -> bool:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from memory_queue import enqueue

        enqueue("flush", payload)
    except Exception:
        return False
    return True


def _enqueue_transcript_fallback(
    transcript_excerpt: str,
    event: str,
    *,
    session_id: str,
    trigger: str,
    project_slug: str,
    project_root: str,
    occurred_at: str,
) -> bool:
    try:
        payload = _build_flush_queue_payload(
            transcript_excerpt,
            event,
            session_id=session_id,
            trigger=trigger,
            project_slug=project_slug,
            project_root=project_root,
            occurred_at=occurred_at,
        )
    except Exception:
        return False
    return payload is not None and _enqueue_flush_payload(payload)


def summarize_with_llm(
    transcript_excerpt: str,
    event: str,
    *,
    session_id: str = "unknown",
    trigger: str = "",
    project_slug: str = "",
    project_root: str = "",
    occurred_at: str = "",
    enqueue_on_unavailable: bool = True,
) -> str:
    """Ask the LLM to classify + distill the transcript into a tier + body."""
    request = _build_flush_queue_payload(
        transcript_excerpt,
        event,
        session_id=session_id,
        trigger=trigger,
        project_slug=project_slug,
        project_root=project_root,
        occurred_at=occurred_at,
    )
    if request is None:
        return ""

    def defer_unavailable() -> None:
        if enqueue_on_unavailable and not _enqueue_flush_payload(request):
            raise RuntimeError("durable flush enqueue failed")

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from llm_client import call_llm
    except ImportError:
        defer_unavailable()
        return ""
    try:
        text = call_llm(
            str(request["prompt"]),
            str(request["system_prompt"]),
            max_tokens=int(request["max_tokens"]),
        )
    except Exception:
        defer_unavailable()
        return ""
    if not text:
        # No backend available (call_llm returned None) — enqueue for
        # deferred processing so the content isn't silently lost as
        # FLUSH_OK. Drained at the next active session via memory_queue.
        defer_unavailable()
        return ""
    return text.strip()


def append_daily(day: str, block: str) -> Path:
    from daily_log_append import locked_append

    out = DAILY_DIR / f"{day}.md"
    locked_append(out, block)
    return out


def append_daily_once(day: str, block: str, marker: str) -> Path:
    from daily_log_append import locked_append_once

    out = DAILY_DIR / f"{day}.md"
    return locked_append_once(out, block, marker)


def dedupe_key(
    session_id: str,
    event: str,
    occurred_at: str,
    project_slug: str,
    project_root: str,
) -> str:
    identity = json.dumps(
        {
            "event": str(event),
            "occurred_at": str(occurred_at),
            "project_root": str(project_root),
            "project_slug": str(project_slug),
            "session_id": str(session_id),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def direct_flush_marker(
    session_id: str,
    event: str,
    occurred_at: str,
    project_slug: str,
    project_root: str,
) -> str:
    key = dedupe_key(
        session_id,
        event,
        occurred_at,
        project_slug,
        project_root,
    )
    return f"<!-- llm-wiki-direct-flush: {key} -->"


def should_skip(
    state: dict,
    session_id: str,
    event: str,
    occurred_at: str,
    project_slug: str,
    project_root: str,
) -> bool:
    key = dedupe_key(
        session_id,
        event,
        occurred_at,
        project_slug,
        project_root,
    )
    last = state.get("flush_dedupe", {}).get(key)
    if not last:
        return False
    return (time.time() - float(last)) < DEDUPE_WINDOW_SECONDS


def record_flush(
    state: dict,
    session_id: str,
    event: str,
    occurred_at: str,
    project_slug: str,
    project_root: str,
) -> None:
    dedupe = state.setdefault("flush_dedupe", {})
    key = dedupe_key(
        session_id,
        event,
        occurred_at,
        project_slug,
        project_root,
    )
    dedupe[key] = time.time()
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
    compiled = trusted_compiled_daily_hashes(state, root=ROOT).get(daily_path.name)
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


def _is_valid_flush_ok_response(raw: str) -> bool:
    return str(raw or "").strip() == "FLUSH_OK"


def _process_flush(
    args: argparse.Namespace,
    occurred_at: str,
    staged_transcript: str | None,
) -> FlushProcessStatus:
    project_slug = str(getattr(args, "project_slug", ""))
    project_root = str(getattr(args, "project_root", ""))

    def result(code: int, durable: bool) -> FlushProcessStatus:
        return FlushProcessStatus(code, durable, project_slug, project_root)

    try:
        identity = _resolve_project_identity(project_slug, project_root)
    except Exception:
        return result(2, False)
    if identity is None:
        return result(0, False)
    project_slug, resolved_project_root = identity
    project_root = str(resolved_project_root)

    # Read-only peek for the dedupe short-circuit; the real write happens
    # inside update_state() below so we don't race with compile_memory.
    try:
        if should_skip(
            load_state(),
            args.session_id,
            args.event,
            occurred_at,
            project_slug,
            project_root,
        ):
            return result(0, True)
    except Exception:
        return result(2, False)

    if staged_transcript is not None:
        transcript = staged_transcript
    elif args.transcript_stdin:
        transcript = read_stream_tail(sys.stdin, MAX_TRANSCRIPT_CHARS)
    else:
        transcript = (
            read_transcript_tail(Path(args.transcript))
            if args.transcript
            else ""
        )
    try:
        raw_summary = (
            summarize_with_llm(
                transcript,
                args.event,
                session_id=args.session_id,
                trigger=args.trigger,
                project_slug=project_slug,
                project_root=project_root,
                occurred_at=occurred_at,
                enqueue_on_unavailable=staged_transcript is None,
            )
            if transcript
            else ""
        )
    except Exception:
        return result(2, False)

    # 3-tier classification (Phase 0.5). Replaces binary FLUSH_OK check.
    tier, body = _classify_response(raw_summary)
    if tier != "ok" and not body:
        print(
            f"flush_memory: FLUSH_{tier.upper()} response has no distilled body",
            file=sys.stderr,
        )
        return result(2, False)

    # FLUSH_OK: nothing worth persisting. Still record dedupe so retries
    # don't hammer the SDK. Still consider auto-compile in case the day's
    # log already has prior MAJOR content worth compiling.
    if tier == "ok":
        if not _is_valid_flush_ok_response(raw_summary):
            # Immediate calls enqueue inside summarize_with_llm when no backend
            # is available. An empty result is durable only on that exact path.
            if not raw_summary and transcript.strip() and staged_transcript is None:
                def _mutate_deferred(state: dict) -> None:
                    record_flush(
                        state,
                        args.session_id,
                        args.event,
                        occurred_at,
                        project_slug,
                        project_root,
                    )

                try:
                    update_state(_mutate_deferred)
                except Exception:
                    # The queue write is already durable; dedupe is secondary.
                    pass
                return result(0, True)
            return result(2, False)

        def _mutate_noop(state: dict) -> None:
            record_flush(
                state,
                args.session_id,
                args.event,
                occurred_at,
                project_slug,
                project_root,
            )
            state.setdefault("flush_empty_count", 0)
            state["flush_empty_count"] = int(state.get("flush_empty_count", 0)) + 1
            state["last_flush_empty_at"] = datetime.now().isoformat(timespec="seconds")
            # Track tier distribution for observability.
            state.setdefault("flush_tier_counts", {})
            state["flush_tier_counts"]["ok"] = int(state["flush_tier_counts"].get("ok", 0)) + 1

        try:
            update_state(_mutate_noop)
        except Exception:
            return result(2, False)
        return result(0, True)

    # FLUSH_MAJOR or FLUSH_MINOR: append the structured body to today's
    # daily log with a Tier: marker so the compile pipeline and human
    # readers can see what kind of content this is.
    marker = direct_flush_marker(
        args.session_id,
        args.event,
        occurred_at,
        project_slug,
        project_root,
    )
    try:
        day, block = render_flush_block(
            tier,
            body,
            event=args.event,
            session_id=args.session_id,
            trigger=args.trigger,
            project_slug=project_slug,
            project_root=project_root,
            occurred_at=occurred_at,
            idempotency_marker=marker,
        )
    except Exception:
        return result(2, False)

    deferred_compiles: list[tuple[Path, str]] = []
    direct_durable = False

    def _mutate(state: dict) -> None:
        nonlocal direct_durable
        if should_skip(
            state,
            args.session_id,
            args.event,
            occurred_at,
            project_slug,
            project_root,
        ):
            direct_durable = True
            return
        daily_path = append_daily_once(day, block, marker)
        direct_durable = True
        record_flush(
            state,
            args.session_id,
            args.event,
            occurred_at,
            project_slug,
            project_root,
        )
        state.setdefault("flush_tier_counts", {})
        state["flush_tier_counts"][tier] = int(state["flush_tier_counts"].get(tier, 0)) + 1
        if tier == "major":
            deferred_compiles.append((daily_path, tier))

    try:
        update_state(_mutate)
    except Exception:
        return result(2, direct_durable)
    if not direct_durable:
        return result(2, False)

    for daily_path, flush_tier in deferred_compiles:
        def _trigger_and_persist(state: dict, _dp=daily_path, _ft=flush_tier) -> None:
            maybe_trigger_compile(state, _dp, _ft)
        try:
            update_state(_trigger_and_persist)
        except Exception:
            return result(2, True)
    return result(0, True)


def _delete_staged_transcript(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def main() -> int:
    args = parse_args()
    source_now = datetime.now().astimezone()
    occurred = _occurred_datetime(getattr(args, "occurred_at", ""), source_now)
    occurred_at = occurred.isoformat(timespec="microseconds")
    staged_transcript: str | None = None
    staged_path: Path | None = None
    if args.delete_transcript and args.transcript:
        staged_path = Path(args.transcript)
        read_result = _read_transcript_tail_result(
            staged_path,
            staged=True,
        )
        if not read_result.successful:
            return 2
        staged_transcript = read_result.text
    try:
        status = _process_flush(args, occurred_at, staged_transcript)
    except Exception:
        status = FlushProcessStatus(
            2,
            False,
            str(getattr(args, "project_slug", "")),
            str(getattr(args, "project_root", "")),
        )
    if staged_path is None:
        return status.code if status.durable else status.code or 2
    if status.durable:
        return status.code if _delete_staged_transcript(staged_path) else 2
    if _enqueue_transcript_fallback(
        staged_transcript or "",
        args.event,
        session_id=args.session_id,
        trigger=args.trigger,
        project_slug=status.project_slug,
        project_root=status.project_root,
        occurred_at=occurred_at,
    ):
        return 0 if _delete_staged_transcript(staged_path) else 2
    return status.code or 2


if __name__ == "__main__":
    raise SystemExit(main())
