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
import contextlib
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from maybe_compile import spawn_compile_if_idle  # noqa: E402
from memory_state import (  # noqa: E402
    ROOT,
    STATE_ROOT,
    file_hash,
    load_state,
    update_state,
)
from secret_redact import redact_secrets  # noqa: E402

DAILY_DIR = ROOT / "knowledge" / "daily"
DEDUPE_WINDOW_SECONDS = 60
MAX_TRANSCRIPT_CHARS = 60_000
# What a session record may read from a transcript file; the record itself is
# bounded again after rendering.
MAX_RECORD_CHARS = 4_000_000
MAX_CAPTURE_INTENT_BYTES = 1024 * 1024
MAX_CAPTURE_DECISION_BYTES = 1024 * 1024
MAX_CAPTURE_TERMINAL_BYTES = 64 * 1024

_CAPTURE_SOURCE_FIELDS = (
    "source_occurrence_id",
    "source_event_id",
    "occurred_at",
    "host",
    "event",
    "session",
    "project_slug",
    "worktree",
    "trigger",
    "checkpoint_reason",
    "chunk_index",
    "chunk_count",
    "evidence",
)
_CAPTURE_SYSTEM_PROMPT = (
    "Classify durable session evidence. Emit exactly FLUSH_OK, or FLUSH_MAJOR "
    "followed by a nonempty body, or FLUSH_MINOR followed by a nonempty body."
)

# Tier sentinels — replace the legacy single FLUSH_OK. The classifier
# is asked to emit exactly one of these as the FIRST line of its
# response, followed (for MAJOR/MINOR) by the structured summary.
TIERS = ("FLUSH_MAJOR", "FLUSH_MINOR", "FLUSH_OK")

# Legacy sentinel still recognized for backward compat with any
# pre-Phase-0.5 summaries that may already be in flight or persisted.
LEGACY_SENTINELS = ("FLUSH_OK", "(no durable content)", "NO_DURABLE_CONTENT")


_NON_FLUSH_PREFIXES = ("(summary failed", '{"operations"', '{"audit"')


def _tier_token(first_line_raw: str) -> str:
    """The first line without the decoration a model likes to add."""
    token = first_line_raw.upper().rstrip(".")
    while token.startswith("`") and token.endswith("`") and len(token) > 1:
        token = token[1:-1]
    return token


def _legacy_ok(stripped: str) -> bool:
    """The old protocol: a bare FLUSH_OK, alone or on a line of its own.

    Both comparisons read the same upper-cased set. The per-line one used to test an
    upper-cased line against the mixed-case sentinels, so `(no durable content)` on a
    line of its own never matched. See
    `docs/research/2026-09-17-the-six-capture-corrections-the-first-round-left.md`.
    """
    sentinels = {sentinel.upper() for sentinel in LEGACY_SENTINELS}
    if stripped.strip(" .\n\t*`").upper() in sentinels:
        return True
    return any(line.strip().upper() in sentinels for line in stripped.splitlines())


def _untiered_response(stripped: str) -> tuple[str, str]:
    """No tier line: the old sentinel, a non-flush payload, or content to keep."""
    if _legacy_ok(stripped) or stripped.startswith(_NON_FLUSH_PREFIXES):
        return "ok", ""
    return "minor", stripped


def _classify_response(raw: str) -> tuple[str, str]:
    """Split the LLM response into (tier, body).

    Accepts the new protocol (first line is FLUSH_MAJOR / FLUSH_MINOR /
    FLUSH_OK) and the legacy protocol (single FLUSH_OK token anywhere).

    Returns:
        ("FLUSH_MAJOR" | "FLUSH_MINOR" | "FLUSH_OK", remaining_text)

    The remaining_text is the structured summary for MAJOR/MINOR tiers
    and is empty for OK. An unrecognised answer becomes MINOR on purpose:
    better to keep content that may be useful than to lose it as OK.
    """
    stripped = (raw or "").strip()
    if not stripped:
        return "ok", ""
    first_line_raw = stripped.splitlines()[0].strip()
    token = _tier_token(first_line_raw)
    if token in TIERS:
        body = stripped[len(first_line_raw) :].strip(" \n\t*`")
        return token.lower().replace("flush_", ""), body
    return _untiered_response(stripped)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--event", required=True, choices=["session-end", "pre-compact"])
    p.add_argument("--session-id", default="unknown")
    p.add_argument("--transcript", default="")
    p.add_argument("--trigger", default="")
    p.add_argument("--source-event-id", default="")
    p.add_argument("--checkpoint-reason", default="")
    p.add_argument(
        "--agent",
        choices=("opencode", "codex", "claude", "unknown"),
        default="unknown",
    )
    p.add_argument("--ephemeral-transcript", action="store_true")
    return p.parse_args()


def _transcript_prefixes() -> list[Path]:
    from host_transcripts import host_transcript_roots

    return [*host_transcript_roots(), STATE_ROOT / "cache" / "transient-transcripts"]


def _is_beneath(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _beneath_any(path: Path, prefixes: list[Path]) -> bool:
    for prefix in prefixes:
        try:
            resolved = prefix.resolve()
        except OSError:
            continue
        if _is_beneath(path, resolved):
            return True
    return False


def _readable_transcript(path: Path) -> Path | None:
    """The resolved path, or None when it is not a plain file we may read."""
    try:
        absolute = path.absolute()
        if not absolute.is_file():
            return None
        if any(candidate.is_symlink() for candidate in (absolute, *absolute.parents)):
            return None
        return path.resolve()
    except OSError:
        return None


def _transcript_path_allowed(path: Path) -> bool:
    """Only allow transcript paths from known agent session directories.

    `transcript_path` arrives from hook JSON (untrusted input). A broad
    allowlist (e.g. all of ``$HOME``) would let a crafted payload point
    at ``~/.ssh/id_rsa`` and ship its contents to the LLM. Instead we
    restrict to exact Claude/Codex session subtrees and the dedicated
    state-root transient cache.
    """
    resolved = _readable_transcript(path)
    if resolved is None:
        return False
    if resolved.suffix not in (".jsonl", ".json", ".txt", ".log"):
        return False
    return _beneath_any(resolved, _transcript_prefixes())


def _cleanup_ephemeral_transcript(path: str) -> None:
    try:
        candidate = Path(path).resolve()
        candidate.relative_to((STATE_ROOT / "cache" / "transient-transcripts").resolve())
        candidate.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def read_transcript_tail(path: Path, max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
    """The last `max_chars` of the transcript.

    Head-and-tail was tried and measured on 2026-08-25 and not adopted. On the
    same forty real sessions both windows promoted 24; two sessions changed
    tier, in opposite directions. The one that got worse is the argument
    against the change: its decisions sat 31 814 characters from the end —
    inside a 60 000-character tail, outside a 30 000-character one — so
    splitting the window dropped exactly the band that carried them.

    So this stays a tail, not because the tail is known to be the right place
    to look, but because nothing measured says moving it helps. See
    `knowledge/notes/session-promotion-policy-decision.md`; what the classifier
    decides was narrowed to one daily-log line in the same change, which is
    what makes this window cheap to be wrong about.
    """
    if not path.exists() or not _transcript_path_allowed(path):
        return ""
    try:
        data = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    return data[-max_chars:] if len(data) > max_chars else data


CLASSIFICATION_SYSTEM_PROMPT = (
    "You classify and distill Claude Code transcripts into a 3-tier "
    "memory scale. Your default bias is toward FLUSH_OK — most "
    "sessions are status chatter and should not pollute the daily "
    "log. You only emit FLUSH_MAJOR when you can point to a concrete "
    "decision or lesson in the transcript. No preamble, no apologies."
)


def build_classification_prompt(transcript_excerpt: str, event: str) -> str:
    """The one prompt that decides a session's tier.

    Extracted so the measurement stand in `benchmark/run_flush_classification.py`
    scores the prompt the product actually sends, not a copy of it.
    """
    return f"""You are classifying + distilling a Claude Code session transcript.

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


def summarize_with_llm(
    transcript_excerpt: str, event: str, session_id: str = ""
) -> str | None:
    """Ask the LLM to classify + distill the transcript into a tier + body.

    Uses the unified llm_client (auto-detected backend — no separate API
    key required on this machine). Returns None only after deferred work is
    durably queued. Raises if neither immediate nor deferred persistence works.
    """
    if not transcript_excerpt.strip():
        return ""
    transcript_excerpt = redact_secrets(transcript_excerpt)

    prompt = build_classification_prompt(transcript_excerpt, event)
    system_prompt = CLASSIFICATION_SYSTEM_PROMPT
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from llm_client import call_llm

        text = call_llm(prompt, system_prompt, max_tokens=1500)
    except Exception:
        text = None
    if not text:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from memory_queue import enqueue

            enqueue("flush", {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "max_tokens": 1500,
                "enqueued_by": "flush_memory",
                "event": event,
                "session_id": session_id,
                "day": datetime.now().strftime("%Y-%m-%d"),
            })
        except Exception as exc:
            raise RuntimeError("flush transcript was not durably persisted") from exc
        return None
    return text.strip()


def _anchor_date(day: str):
    from datetime import date

    try:
        return date.fromisoformat(day)
    except (TypeError, ValueError):
        return None


def _dated_block(day: str, block: str) -> str:
    """The entry, followed by the dates it mentions, resolved against its day.

    A session says "last Thursday" and the entry records the day it was
    captured; nothing joined the two, so a question about that Thursday found
    both halves and no sentence stating the answer. Measured 2026-09-03, four of
    nine substantive refusals on the stand were exactly this. Resolving here
    makes the date ordinary citable text, and every gate downstream is unchanged.
    """
    from temporal_anchor import annotation

    anchor = _anchor_date(day)
    if anchor is None:
        return block
    return block + annotation(block, anchor)


def append_daily(day: str, block: str, operation_id: str | None = None) -> Path:
    from daily_log_append import locked_append

    out = DAILY_DIR / f"{day}.md"
    locked_append(out, _dated_block(day, block), operation_id=operation_id)
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


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _elapsed_since(text: str) -> float:
    try:
        last = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return float("inf")
    return (datetime.now() - last).total_seconds()


def _within_cooldown(state: dict) -> bool:
    """On a busy day every session-end after the cutoff would else re-spawn compile.

    Tune with MEMORY_COMPILE_COOLDOWN_SECONDS (default 900); 0 disables it.
    """
    cooldown_seconds = _env_int("MEMORY_COMPILE_COOLDOWN_SECONDS", 900)
    if cooldown_seconds <= 0:
        return False
    last_spawned = state.get("last_compile_spawned_at")
    if not last_spawned:
        return False
    return _elapsed_since(str(last_spawned)) < cooldown_seconds


def _compile_is_due(state: dict, daily_path: Path, tier: str) -> bool:
    if tier != "major" or datetime.now().hour < _env_int(
        "MEMORY_COMPILE_AFTER_HOUR", 18
    ):
        return False
    compiled = state.get("compiled_daily_hashes", {}).get(daily_path.name)
    return compiled != file_hash(daily_path) and not _within_cooldown(state)


def _record_compile_trigger(
    state: dict,
    daily_path: Path,
    tier: str,
    spawned_at: str,
    spawned: bool,
    reason: str,
) -> None:
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


def maybe_trigger_compile(state: dict, daily_path: Path, tier: str) -> None:
    """Spawn compile only for FLUSH_MAJOR content, after the hour cutoff.

    Always goes through `maybe_compile.spawn_compile_if_idle` so the PID
    lock is the single concurrency gate (hooks / wrappers / schedulers
    must not spawn `compile_memory.py` directly).
    """
    if not _compile_is_due(state, daily_path, tier):
        return
    spawned_at = datetime.now().isoformat(timespec="seconds")
    spawned, reason = spawn_compile_if_idle(force=False)
    if spawned:
        state["last_compile_spawned_at"] = spawned_at
    _record_compile_trigger(state, daily_path, tier, spawned_at, spawned, reason)


def _flush_summary(args: argparse.Namespace) -> str | None:
    """Classify the conversation in the transcript, not the JSON around it.

    The whole file, then rendered, then bounded — in that order. Reading a
    60 000-character tail of raw JSONL first can land inside a single
    bookkeeping entry and hand the classifier no conversation at all; the
    rendered conversation of a real session is 2 KB to 32 KB and fits the
    window whole. Same reasoning as the durable record a few hundred lines
    down, and the same reasoning as `_readable_evidence`.
    """
    if not args.transcript:
        return ""
    transcript = read_transcript_tail(Path(args.transcript), max_chars=MAX_RECORD_CHARS)
    if not transcript:
        return ""
    readable = _bounded_classifier_evidence(_readable_evidence(transcript))
    return summarize_with_llm(readable, args.event, args.session_id)


def _capture_binding_intent_id(binding: object) -> str:
    intent_id = getattr(binding, "intent_id", None)
    if not isinstance(intent_id, str):
        raise RuntimeError("capture intent is unresolved")
    return intent_id


def _require_bound_intent(intent_id: object, intent_sha256: object) -> None:
    """The binding names both halves of the intent identity, or neither."""
    if not isinstance(intent_id, str) or not isinstance(intent_sha256, str):
        raise RuntimeError("capture task binding is invalid")


def _capture_intent_reference(
    lease: object, active: object
) -> tuple[str, str, str]:
    payload = getattr(lease, "payload", None)
    intent_id = getattr(active, "intent_id", None)
    intent_sha256 = getattr(active, "intent_sha256", None)
    if not isinstance(payload, Mapping):
        raise RuntimeError("capture task payload is invalid")
    _require_bound_intent(intent_id, intent_sha256)
    intent_path = f"run/capture-intents/ready/{intent_id[:2]}/{intent_id}.json"
    actual = (
        set(payload),
        payload.get("intent_id"),
        payload.get("intent_sha256"),
        isinstance(payload.get("intent_path"), str),
        getattr(active, "task_id", None),
    )
    expected = (
        {"intent_id", "intent_path", "intent_sha256"},
        intent_id,
        intent_sha256,
        True,
        getattr(lease, "id", None),
    )
    if actual != expected:
        raise RuntimeError("capture task payload conflicts with its binding")
    return intent_id, intent_path, intent_sha256


def _decode_capture_intent(data: bytes) -> dict[str, object]:
    from reliable_memory import canonical_json_bytes, validate_schema

    try:
        record = json.loads(data.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("capture intent JSON is invalid") from exc
    if not isinstance(record, dict):
        raise RuntimeError("capture intent must be a JSON object")
    validate_schema(record, Path(__file__).with_name("schemas") / "capture-intent-v1.json")
    if canonical_json_bytes(record) != data:
        raise RuntimeError("capture intent is not canonical JSON")
    return record


def _require_capture_intent_identity(record: Mapping[str, object]) -> None:
    from reliable_memory import canonical_json_bytes, sha256_bytes

    source = {field: record[field] for field in _CAPTURE_SOURCE_FIELDS}
    chunk_sha256 = sha256_bytes(canonical_json_bytes(record["evidence"]))
    complete_sha256 = sha256_bytes(canonical_json_bytes(source))
    identity = {
        "schema_version": "capture-intent/v1",
        "source_occurrence_id": record["source_occurrence_id"],
        "source_event_id": record["source_event_id"],
        "occurred_at": record["occurred_at"],
        "checkpoint_reason": record["checkpoint_reason"],
        "chunk_index": record["chunk_index"],
        "chunk_sha256": chunk_sha256,
    }
    actual = (
        record["chunk_sha256"],
        record["complete_input_sha256"],
        record["intent_id"],
    )
    expected = (
        chunk_sha256,
        complete_sha256,
        sha256_bytes(canonical_json_bytes(identity)),
    )
    if actual != expected:
        raise RuntimeError("capture intent identity is invalid")


def _read_capture_intent(
    queue: object, lease: object, active: object
) -> dict[str, object]:
    from reliable_memory import read_runtime_bytes, sha256_bytes

    intent_id, intent_path, intent_sha256 = _capture_intent_reference(lease, active)
    data = read_runtime_bytes(
        queue.state_root / intent_path,
        queue.state_root,
        max_bytes=MAX_CAPTURE_INTENT_BYTES,
        owner_only=True,
    )
    if sha256_bytes(data) != intent_sha256:
        raise RuntimeError("capture intent digest changed")
    record = _decode_capture_intent(data)
    if record["intent_id"] != intent_id:
        raise RuntimeError("capture intent ID conflicts with its binding")
    _require_capture_intent_identity(record)
    return record


def _readable_evidence(evidence: object) -> str:
    """The conversation this evidence carries, not the JSON that carries it.

    The classifier used to read `canonical_json_bytes(evidence)`: the host's
    raw JSONL, every bookkeeping line included, and only the last 60 000
    characters of it. Measured on the 187 ready intents on this vault, a
    session's evidence is 143 KB to 942 KB of that, and the conversation
    inside it renders to 2 KB to 32 KB. So the window held about six per cent
    of the bytes, taken from the end, and on the intents inspected on
    2026-09-06 it was file-backup manifests and token-cost accounting from
    edge to edge — not one line of anyone speaking.

    That is the whole explanation of `flush_tier_counts: {"ok": 65}`. Sixty-five
    sessions in a row were classified as having nothing worth saving, and each
    verdict was right about the bytes it was shown. Rendered, the same
    conversation fits the window whole with room to spare, and it is the same
    rendering the durable session record already keeps.
    """
    from session_evidence import evidence_text, render_transcript

    if isinstance(evidence, str):
        return render_transcript(evidence)
    return render_transcript(evidence_text(evidence))


def _bounded_classifier_evidence(evidence: str) -> str:
    """The tail the classifier reads; the durable record keeps all the evidence it was given.

    Not "complete": the evidence of a very long session is itself a head and a
    tail, and says so in a `capture_gap` line.

    Measured 2026-08-28 on the 13 live capture intents: the unbounded prompt
    reached a median of 723 288 characters per session — the 60 000-character
    classifier window of `session-promotion-policy-decision` was bypassed by
    the intent path, which carries up to 1 MiB. A tail, not head+tail: the
    same decision measured head+tail on forty real sessions and rejected it.
    """
    if len(evidence) <= MAX_TRANSCRIPT_CHARS:
        return evidence
    dropped = len(evidence) - MAX_TRANSCRIPT_CHARS
    return (
        f"[…{dropped} characters of earlier evidence omitted for "
        f"classification; the stored record keeps them]"
        + evidence[-MAX_TRANSCRIPT_CHARS:]
    )


def _capture_prompt(record: Mapping[str, object]) -> str:
    evidence = _readable_evidence(record["evidence"])
    return (
        "Classify this role-preserved session evidence using the closed flush grammar.\n"
        f"Event: {record['event']}\n"
        f"Evidence: {_bounded_classifier_evidence(evidence)}"
    )


# Emphasis a model puts around the tier it is declaring. The grammar is closed
# on the token, not on the punctuation a Markdown-speaking model wraps it in:
# on the first five real intents classified with a readable window, two came
# back as `**FLUSH_MAJOR**`, and under the literal rule both sessions would
# have been destroyed as invalid output.
_TIER_EMPHASIS = "*_`# "


# What may follow a tier on its own line: the full stop, colon or exclamation a
# model ends a declaration with. See
# `docs/research/2026-09-14-a-capture-keeps-its-claim-while-it-asks.md`.
_TIER_TRAILING = ".:!"
_TIERS_BY_TOKEN = {"FLUSH_OK": "ok", "FLUSH_MAJOR": "major", "FLUSH_MINOR": "minor"}


def _line_tier(line: str) -> tuple[str, str] | None:
    """(tier, text after the token on this line), or None when the line declares none."""
    token, _, inline = line.strip().strip(_TIER_EMPHASIS).partition(":")
    tier = _TIERS_BY_TOKEN.get(token.strip().strip(_TIER_EMPHASIS).rstrip(_TIER_TRAILING))
    if tier is None:
        return None
    return tier, inline.strip().strip(_TIER_EMPHASIS)


def _declared_tier(raw: str) -> tuple[str, str] | None:
    """The tier the first non-blank line declares, and the body it carries.

    Only the first line may declare: the classifier reads untrusted transcripts,
    and a tier token quoted from one further down must not decide. Text after
    `FLUSH_OK` is the model saying why nothing is kept, and is not a body.
    """
    head, _, rest = raw.lstrip().partition("\n")
    declared = _line_tier(head)
    if declared is None:
        return None
    return declared[0], "\n".join([declared[1], rest])


def _require_canonical_body(body: str) -> str:
    """The flush body, with the whitespace around it removed.

    This used to refuse a body that was not already stripped, and the refusal
    lost the whole capture: nine sessions on this vault were recorded under
    `noncanonical flush output`, and every one of them was a model that ended
    its answer with a newline. Whitespace around a Markdown body carries
    nothing a reader or a later grep can use, so refusing it protects nothing
    and costs a session. Output that is not a flush body at all is still
    refused: a body that is only whitespace here, and a reply that declares no
    tier anywhere in `_parse_capture_wire_output`.
    """
    stripped = body.strip()
    if not stripped:
        raise RuntimeError("capture provider returned an empty flush body")
    return stripped


def _required_tier(raw: object) -> tuple[str, str]:
    if not isinstance(raw, str):
        raise RuntimeError("capture provider returned no flush output")
    declared = _declared_tier(raw)
    if declared is None:
        raise RuntimeError("capture provider returned invalid flush output")
    return declared


def _parse_capture_wire_output(raw: object) -> tuple[str, str]:
    tier, body = _required_tier(raw)
    if tier == "ok":
        return "ok", ""
    return tier, _require_canonical_body(body)


# A third of the shortest claim a capture holds (the intent fence's 30 s). See
# `docs/research/2026-09-14-a-capture-keeps-its-claim-while-it-asks.md`.
CAPTURE_KEEPALIVE_SECONDS = 10.0


class _CaptureKeepAlive:
    """Renew every claim a capture holds while its classifier runs.

    Owner and its queue projection, the queue lease, the task fence and the
    intent fence: nothing renewed them, and no capture over 30 seconds ever
    succeeded. A busy database is retried until the shortest claim expires;
    publication stays fenced, so a claim that was lost still refuses to publish.
    """

    def __init__(self, queue, coordinator, lease, task_fence, intent_fence, owner) -> None:
        self._queue = queue
        self._coordinator = coordinator
        self._lease = lease
        self._task_fence = task_fence
        self._intent_fence = intent_fence
        self._owner = owner
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="capture-keepalive", daemon=True)

    def __enter__(self) -> _CaptureKeepAlive:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=CAPTURE_KEEPALIVE_SECONDS * 2)

    def _run(self) -> None:
        from lease_renewal import renew_until_stopped
        from markdown_transaction import INTENT_FENCE_SECONDS
        from reliable_memory import DEFAULTS

        # The ending error needs no handling here: publication stays fenced, so a
        # claim that ran out refuses to publish on its own. One round renews the
        # queue and the coordinator; the coordinator's busy wait is the longer.
        renew_until_stopped(
            self._renew,
            interval=CAPTURE_KEEPALIVE_SECONDS,
            lease_seconds=INTENT_FENCE_SECONDS,
            attempt_seconds=DEFAULTS.markdown_busy_ms / 1_000,
            stop=self._stop,
        )

    def _renew(self) -> None:
        self._owner = self._queue.heartbeat_queue_owner(self._owner)
        self._lease = self._queue.heartbeat(self._lease)
        self._queue.heartbeat_task_fence(self._task_fence, self._owner)
        self._coordinator.heartbeat_intent_fence(self._intent_fence, self._owner)


class CaptureProviderUnavailable(RuntimeError):
    """The provider did not answer: the capture waits for it, it has not failed.

    See `docs/research/2026-09-17-an-absent-provider-is-waited-for-and-a-spent-task-is-a-loss.md`.
    """


# How long a capture waits for a provider that did not answer. Eight attempts an
# hour apart span most of a working day; with no stated wait they were spent in
# about an hour, which is shorter than one subscription usage window.
PROVIDER_RETRY_SECONDS = 3600


def _call_capture_classifier(
    record: Mapping[str, object],
    llm_call: Callable[[str, str, int], object] | None,
) -> tuple[object, str, str]:
    from llm_client import LLMResult, call_llm_result

    caller = llm_call if llm_call is not None else call_llm_result
    result = caller(_capture_prompt(record), _CAPTURE_SYSTEM_PROMPT, 1500)
    if not isinstance(result, LLMResult):
        raise RuntimeError("capture provider did not return a provider result")
    if (result.available, result.failure_class) != (True, None):
        raise CaptureProviderUnavailable("capture provider call did not succeed")
    tier, body = _parse_capture_wire_output(result.text)
    return result, tier, body


def _capture_tier_outcome(tier: str) -> str:
    outcomes = {
        "ok": "semantic_ok",
        "major": "major_written",
        "minor": "minor_written",
    }
    try:
        return outcomes[tier]
    except KeyError as exc:
        raise ValueError("capture tier is invalid") from exc


def _capture_now() -> datetime:
    return datetime.now().astimezone()


def _require_capture_time(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("capture decision time must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("capture decision time must be timezone-aware")
    return value


def _capture_text(value: object, fallback: str) -> str:
    if isinstance(value, str) and value:
        return value
    return fallback


def _capture_daily_block(
    record: Mapping[str, object], tier: str, body: str, chosen_at: datetime
) -> str:
    event = _capture_text(record["event"], "session_end").replace("_", "-")
    session = _capture_text(record["session"], "unknown")
    trigger = _capture_text(record["trigger"], event)
    header = f"\n## [{chosen_at.strftime('%H:%M:%S')}] {event} | {session}\n"
    metadata = (
        f"- Trigger: `{trigger}`\n"
        f"- Agent: `{record['host']}`\n"
        f"- Capture intent: `{record['intent_id']}`\n"
        f"- Tier: `{tier}`\n\n"
    )
    return redact_secrets(f"{header}{metadata}{body}\n")


def _dated_capture_block(
    record: Mapping[str, object], tier: str, body: str, chosen_at: datetime
) -> str:
    """The block, followed by the dates it mentions resolved against its own day.

    The live path never passed through `append_daily`, where that step lived, so no
    queued entry had its dates resolved. See
    `docs/research/2026-09-17-a-queued-entry-resolves-its-dates-too.md`.
    """
    block = _capture_daily_block(record, tier, body, chosen_at)
    return _dated_block(chosen_at.strftime("%Y-%m-%d"), block)


# `False` is the form every decision stored before 2026-09-17 recorded; it is only
# ever rebuilt to recognise one of those.
_CAPTURE_BLOCK_BUILDERS = {True: _dated_capture_block, False: _capture_daily_block}


def _capture_operation_plan(
    record: Mapping[str, object],
    tier: str,
    body: str,
    chosen_at: datetime | None,
    *,
    dated: bool = True,
) -> list[dict[str, object]]:
    from reliable_memory import sha256_bytes

    if tier == "ok":
        return []
    if chosen_at is None:
        raise ValueError("durable capture decision requires a chosen time")
    chosen = _require_capture_time(chosen_at)
    build_block = _CAPTURE_BLOCK_BUILDERS[dated]
    block = build_block(record, tier, body, chosen)
    path = f"knowledge/daily/{chosen.strftime('%Y-%m-%d')}.md"
    return [
        {
            "kind": "append",
            "path": path,
            "block": block,
            "block_sha256": sha256_bytes(block.encode("utf-8")),
            "operation_id": f"capture-markdown:{record['intent_id']}",
            "chosen_at": chosen.isoformat().replace("+00:00", "Z"),
        }
    ]


def _capture_decision_time(decision: Mapping[str, object]) -> datetime | None:
    plan = decision["operation_plan"]
    if not plan:
        return None
    try:
        value = datetime.fromisoformat(str(plan[0]["chosen_at"]).replace("Z", "+00:00"))
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("capture decision time is invalid") from exc
    return _require_capture_time(value)


def _require_capture_decision_semantics(
    decision: Mapping[str, object], intent: Mapping[str, object]
) -> None:
    tier, body = _parse_capture_wire_output(decision["wire_output"])
    actual = (decision["tier"], decision["outcome"])
    expected = (tier, _capture_tier_outcome(tier))
    if actual != expected:
        raise RuntimeError("capture decision outcome is invalid")
    chosen_at = _capture_decision_time(decision)
    # A decision stored before the dates were resolved is still a valid decision.
    plans = [
        _capture_operation_plan(intent, tier, body, chosen_at, dated=dated)
        for dated in (True, False)
    ]
    if decision["operation_plan"] not in plans:
        raise RuntimeError("capture decision operation plan is invalid")


def _capture_decision_bytes(
    record: Mapping[str, object],
    active: object,
    result: object,
    tier: str,
    body: str,
    chosen_at: datetime | None,
) -> bytes:
    from llm_client import LLMResult
    from reliable_memory import canonical_json_bytes, validate_schema

    if not isinstance(result, LLMResult):
        raise TypeError("capture decision requires an LLM result")
    descriptor = result.descriptor
    decision = {
        "schema_version": "capture-decision/v1",
        "intent_id": record["intent_id"],
        "intent_sha256": getattr(active, "intent_sha256"),
        "complete_input_sha256": record["complete_input_sha256"],
        "chunk_sha256": record["chunk_sha256"],
        "stage": "flush",
        "provider": {
            "provider": descriptor.provider,
            "model": descriptor.model,
            "candidate_index": descriptor.candidate_index,
            "fallback_from": list(descriptor.fallback_from),
            "structured_output": result.structured_output,
        },
        "wire_output": result.text,
        "tier": tier,
        "outcome": _capture_tier_outcome(tier),
        "operation_plan": _capture_operation_plan(record, tier, body, chosen_at),
        "processing_binding": {
            "kind": "task",
            "task_id": getattr(active, "task_id"),
            "active_link_digest": getattr(active, "active_digest"),
        },
    }
    schema = Path(__file__).with_name("schemas") / "capture-decision-v1.json"
    validate_schema(decision, schema)
    _require_capture_decision_semantics(decision, record)
    encoded = canonical_json_bytes(decision)
    if len(encoded) > MAX_CAPTURE_DECISION_BYTES:
        raise RuntimeError("capture decision exceeds its byte limit")
    return encoded


def _ensure_capture_results_directory(queue: object) -> None:
    from reliable_memory import fsync_directory

    path = Path(queue.results_dir)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    else:
        fsync_directory(path.parent)
    if path.is_symlink() or not path.is_dir():
        raise PermissionError("capture results directory is unsafe")
    path.resolve(strict=True).relative_to(Path(queue.state_root).resolve(strict=True))


def _capture_decision_relative_path(intent_id: str) -> str:
    from reliable_memory import canonical_json_bytes, sha256_bytes

    key = sha256_bytes(
        canonical_json_bytes({"intent_id": intent_id, "stage": "flush"})
    )
    return f"run/queue-results/capture-decision-{key}.json"


def _index_capture_decision(
    queue: object,
    coordinator: object,
    lease: object,
    active: object,
    task_fence: object,
    intent_fence: object,
    owner: object,
    encoded: bytes,
) -> object:
    from reliable_memory import publish_runtime_file, sha256_bytes

    relative = _capture_decision_relative_path(active.intent_id)
    publish_runtime_file(
        queue.state_root / relative,
        encoded,
        state_root=queue.state_root,
        create_only=True,
    )
    return queue.publish_semantic_decision(
        coordinator,
        task_id=lease.id,
        intent_id=active.intent_id,
        stage="flush",
        decision_path=relative,
        decision_sha256=sha256_bytes(encoded),
        active_link_digest=active.active_digest,
        task_fence=task_fence,
        intent_fence=intent_fence,
        owner=owner,
    )


def _decode_capture_decision(data: bytes) -> dict[str, object]:
    from reliable_memory import canonical_json_bytes, validate_schema

    try:
        decision = json.loads(data.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("capture decision JSON is invalid") from exc
    if not isinstance(decision, dict):
        raise RuntimeError("capture decision must be a JSON object")
    schema = Path(__file__).with_name("schemas") / "capture-decision-v1.json"
    validate_schema(decision, schema)
    if canonical_json_bytes(decision) != data:
        raise RuntimeError("capture decision is not canonical JSON")
    return decision


def _require_capture_decision_identity(
    decision: Mapping[str, object],
    intent: Mapping[str, object],
    active: object,
) -> None:
    actual = (
        decision["intent_id"],
        decision["intent_sha256"],
        decision["complete_input_sha256"],
        decision["chunk_sha256"],
        decision["processing_binding"],
    )
    expected = (
        active.intent_id,
        active.intent_sha256,
        intent["complete_input_sha256"],
        intent["chunk_sha256"],
        {
            "kind": "task",
            "task_id": active.task_id,
            "active_link_digest": active.active_digest,
        },
    )
    if actual != expected:
        raise RuntimeError("capture decision conflicts with its binding")
    _require_capture_decision_semantics(decision, intent)


def _existing_capture_decision(
    queue: object,
    coordinator: object,
    lease: object,
    active: object,
    task_fence: object,
    intent_fence: object,
    owner: object,
    intent: Mapping[str, object],
) -> tuple[object, dict[str, object]] | None:
    indexed = queue.indexed_capture_decision(
        task_id=lease.id,
        intent_id=active.intent_id,
        stage="flush",
        active_link_digest=active.active_digest,
    )
    relative = _capture_decision_relative_path(active.intent_id)
    candidate = queue.state_root / relative
    try:
        candidate.lstat()
    except FileNotFoundError:
        return None
    from reliable_memory import read_runtime_bytes

    encoded = read_runtime_bytes(
        candidate,
        queue.state_root,
        max_bytes=MAX_CAPTURE_DECISION_BYTES,
        owner_only=True,
    )
    decision = _decode_capture_decision(encoded)
    _require_capture_decision_identity(decision, intent, active)
    if indexed is None:
        indexed = _index_capture_decision(
            queue,
            coordinator,
            lease,
            active,
            task_fence,
            intent_fence,
            owner,
            encoded,
        )
    return indexed, decision


def _publish_capture_decision(
    queue: object,
    coordinator: object,
    lease: object,
    active: object,
    task_fence: object,
    intent_fence: object,
    owner: object,
    record: Mapping[str, object],
    result: object,
    tier: str,
    body: str,
    chosen_at: datetime | None,
) -> tuple[object, dict[str, object]]:
    encoded = _capture_decision_bytes(
        record, active, result, tier, body, chosen_at
    )
    indexed = _index_capture_decision(
        queue,
        coordinator,
        lease,
        active,
        task_fence,
        intent_fence,
        owner,
        encoded,
    )
    return indexed, _decode_capture_decision(encoded)


def _capture_terminal_bytes(
    active: object, decision: object, disposition: Mapping[str, object]
) -> bytes:
    from reliable_memory import canonical_json_bytes

    terminal = {
        "schema_version": "capture-terminal/v1",
        "intent_id": active.intent_id,
        "intent_sha256": active.intent_sha256,
        "semantic_decisions": [
            {
                "stage": decision.stage,
                "decision_path": decision.decision_path,
                "decision_sha256": decision.decision_sha256,
            }
        ],
        "processing_binding": {
            "kind": "task",
            "task_id": active.task_id,
            "active_link_digest": active.active_digest,
        },
        "disposition": dict(disposition),
    }
    encoded = canonical_json_bytes(terminal)
    if len(encoded) > MAX_CAPTURE_TERMINAL_BYTES:
        raise RuntimeError("capture terminal exceeds its byte limit")
    return encoded


def _publish_capture_terminal(
    queue: object,
    lease: object,
    active: object,
    task_fence: object,
    intent_fence: object,
    owner: object,
    decision: object,
    disposition: Mapping[str, object],
) -> object:
    from reliable_memory import publish_runtime_file, sha256_bytes

    encoded = _capture_terminal_bytes(active, decision, disposition)
    relative = f"run/queue-results/capture-{active.intent_id}.json"
    publish_runtime_file(
        queue.state_root / relative,
        encoded,
        state_root=queue.state_root,
        create_only=True,
    )
    return queue.complete_capture_terminal(
        lease,
        intent_id=active.intent_id,
        terminal_path=relative,
        terminal_sha256=sha256_bytes(encoded),
        active_link_digest=active.active_digest,
        task_fence=task_fence,
        intent_fence=intent_fence,
        owner=owner,
    )


def _publish_no_content_terminal(
    queue: object,
    lease: object,
    active: object,
    task_fence: object,
    intent_fence: object,
    owner: object,
    decision: object,
) -> object:
    disposition = {
        "kind": "no_durable_content",
        "decision_sha256": decision.decision_sha256,
    }
    return _publish_capture_terminal(
        queue,
        lease,
        active,
        task_fence,
        intent_fence,
        owner,
        decision,
        disposition,
    )


def _capture_transaction_preconditions(active: object, intent_fence: object) -> dict:
    return {
        "intent_fence": {
            "intent_id": intent_fence.intent_id,
            "mode": intent_fence.mode,
            "token": intent_fence.token,
            "fencing_epoch": intent_fence.epoch,
            "expires_at": intent_fence.expires_at.isoformat().replace("+00:00", "Z"),
        },
        "capture_binding": {
            "intent_id": active.intent_id,
            "task_id": active.task_id,
            "active_link_digest": active.active_digest,
            "seal_digest": active.seal_digest,
        },
    }


def _commit_capture_markdown(
    queue: object,
    coordinator: object,
    lease: object,
    active: object,
    intent_fence: object,
    owner: object,
    decision: object,
    decision_record: Mapping[str, object],
) -> tuple[object, object]:
    from markdown_transaction import append_captured_knowledge

    sealed = queue.active_capture_binding(None, lease.id)
    if sealed.seal_digest is None or sealed.active_digest != active.active_digest:
        raise RuntimeError("capture decision did not seal the active binding")
    coordinator.project_capture_binding(sealed, intent_fence=intent_fence)
    plan = decision_record["operation_plan"][0]
    transaction = append_captured_knowledge(
        coordinator,
        owner,
        plan["operation_id"],
        coordinator.vault / plan["path"],
        plan["block"].encode("utf-8"),
        preconditions=_capture_transaction_preconditions(sealed, intent_fence),
    )
    if transaction.state != "committed":
        raise RuntimeError("capture Markdown transaction did not commit")
    return sealed, transaction


def _capture_markdown_disposition(transaction: object, decision: object) -> dict:
    outputs = [
        {"path": operation.path, "sha256": operation.after_hash}
        for operation in transaction.operations
    ]
    return {
        "kind": "markdown_committed",
        "transaction_id": transaction.id,
        "operation_id": transaction.operation_id,
        "decision_sha256": decision.decision_sha256,
        "outputs": outputs,
    }


def _complete_capture_decision(
    queue: object,
    coordinator: object,
    lease: object,
    active: object,
    task_fence: object,
    intent_fence: object,
    owner: object,
    decision: object,
    decision_record: Mapping[str, object],
) -> object:
    if decision_record["outcome"] == "semantic_ok":
        return _publish_no_content_terminal(
            queue,
            lease,
            active,
            task_fence,
            intent_fence,
            owner,
            decision,
        )
    sealed, transaction = _commit_capture_markdown(
        queue,
        coordinator,
        lease,
        active,
        intent_fence,
        owner,
        decision,
        decision_record,
    )
    disposition = _capture_markdown_disposition(transaction, decision)
    return _publish_capture_terminal(
        queue,
        lease,
        sealed,
        task_fence,
        intent_fence,
        owner,
        decision,
        disposition,
    )


def _keep_session_record(
    record: Mapping[str, object],
    now: Callable[[], datetime],
    coordinator: object | None = None,
    owner: object | None = None,
) -> None:
    """Keep the session itself before anything judges it.

    Retention must not depend on the tier: measured on this vault's own sessions,
    the classifier answered "nothing worth keeping" 39 times out of 40, and a
    controlled 2026 ablation puts a 16-to-22-point retrieval cost on deciding
    relevance at write time. See knowledge/notes/session-evidence-retention-decision.md.
    """
    from session_evidence import evidence_text, intent_fields, write_session_evidence

    evidence = record.get("evidence")
    if not isinstance(evidence, Sequence):
        return
    captured_at = _session_time(record, now).isoformat()
    write_session_evidence(
        ROOT,
        intent_fields(record, captured_at),
        evidence_text(evidence),
        coordinator=coordinator,
        owner=owner,
    )


def _capture_time_text(now: Callable[[], datetime]) -> str:
    try:
        return _require_capture_time(now()).isoformat()
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc).isoformat()


# How far from now an intent's own timestamp may sit and still be believed. Beyond
# this it is a broken clock rather than a late session, and filing by it would
# scatter entries across arbitrary days. See
# `docs/research/2026-09-17-a-session-is-filed-under-the-day-it-happened.md`.
MAX_BACKDATED_CAPTURE_DAYS = 30


def _believable_session_time(occurred: datetime, moment: datetime) -> datetime | None:
    if abs((moment - occurred).days) > MAX_BACKDATED_CAPTURE_DAYS:
        return None
    return occurred.astimezone()


def _intent_time(record: Mapping[str, object]) -> datetime | None:
    """The moment the session itself ended, when the intent carries one."""
    raw = record.get("occurred_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        occurred = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if occurred.tzinfo is None:
        return None
    return occurred


def _session_time(record: Mapping[str, object], now: Callable[[], datetime]) -> datetime:
    """The session's own time when it has a believable one, else the worker's clock.

    Both the session record's day and the daily entry's day come from here, so a
    backlog drained the next morning files each session under the day it happened and
    a retry after midnight chooses the same day as the first attempt.
    """
    moment = _require_capture_time(now())
    occurred = _intent_time(record)
    if occurred is None:
        return moment
    return _believable_session_time(occurred, moment) or moment


def _keep_transcript_record(args: argparse.Namespace) -> None:
    """The same record for the detached flush path, which reads the file itself."""
    from session_evidence import write_session_evidence

    if not args.transcript:
        return
    # The whole file, not the classifier's tail: a transcript's entries can each
    # be tens of thousands of characters, so a 60k tail can start inside one and
    # leave no complete line to render. The record is for storage, not for a
    # context window, and the rendered result is bounded on its own.
    transcript = read_transcript_tail(Path(args.transcript), max_chars=MAX_RECORD_CHARS)
    if not transcript:
        return
    fields = {
        "session": args.session_id,
        "host": getattr(args, "agent", None),
        "event": args.event,
        # The queue path's clock: one day for a session whichever path records it.
        # See `docs/research/2026-09-14-one-day-for-a-session-record.md`.
        "captured_at": _capture_now().isoformat(),
        "source_event_id": getattr(args, "source_event_id", None),
    }
    write_session_evidence(ROOT, fields, transcript)


def process_new_capture(
    queue: object,
    coordinator: object,
    lease: object,
    active: object,
    task_fence: object,
    intent_fence: object,
    owner: object,
    *,
    llm_call: Callable[[str, str, int], object] | None = None,
    now: Callable[[], datetime] = _capture_now,
) -> object:
    record = _read_capture_intent(queue, lease, active)
    _keep_session_record(record, now, coordinator, owner)
    _ensure_capture_results_directory(queue)
    resolved = _existing_capture_decision(
        queue, coordinator, lease, active, task_fence, intent_fence, owner, record
    )
    if resolved is None:
        with _CaptureKeepAlive(queue, coordinator, lease, task_fence, intent_fence, owner):
            result, tier, body = _call_capture_classifier(record, llm_call)
        chosen_at = None
        if tier != "ok":
            chosen_at = _session_time(record, now)
        resolved = _publish_capture_decision(
            queue,
            coordinator,
            lease,
            active,
            task_fence,
            intent_fence,
            owner,
            record,
            result,
            tier,
            body,
            chosen_at,
        )
    decision, decision_record = resolved
    return _complete_capture_decision(
        queue,
        coordinator,
        lease,
        active,
        task_fence,
        intent_fence,
        owner,
        decision,
        decision_record,
    )


def process_capture_lease(
    queue: object,
    coordinator: object,
    lease: object,
    *,
    owner: object,
    process_missing: Callable[[object, object, object, object, object], object],
) -> object:
    from memory_queue import capture_task_fences

    binding = queue.active_capture_binding(None, lease.id)
    intent_id = _capture_binding_intent_id(binding)
    with capture_task_fences(
        queue,
        coordinator,
        lease.id,
        intent_id=intent_id,
        mode="worker",
        owner=owner,
    ) as (task_fence, intent_fence):
        if intent_fence is None:
            raise RuntimeError("capture intent fence is unavailable")
        terminal = queue.complete_existing_capture_terminal(
            lease,
            intent_id=intent_id,
            active_link_digest=binding.active_digest,
            task_fence=task_fence,
            intent_fence=intent_fence,
            owner=owner,
        )
        if terminal is not None:
            return terminal
        return process_missing(lease, binding, task_fence, intent_fence, owner)


def _adopt_orphaned_intents(queue: object, coordinator: object) -> None:
    """Give a task to any intent that was published but never dispatched.

    This runs before the owner below is taken, because adoption needs its own
    capture owner per intent, and it runs before `claim_capture` so an adopted
    intent is claimable in this same pass. It is best effort by contract: the
    worker's job is to drain the queue, and a sweeper that cannot run must never
    be the reason the queue is not drained.
    """
    from capture_adoption import (
        adopt_orphaned_capture_intents,
        complete_pending_capture_intents,
    )

    for sweep in (complete_pending_capture_intents, adopt_orphaned_capture_intents):
        _swept_intents(sweep, queue, coordinator)


def _swept_intents(sweep, queue: object, coordinator: object) -> None:
    """One recovery pass, best effort: a sweeper that fails never stops the drain.

    Two passes run here. One finishes a publication that stopped half way — a
    `pending` row whose publisher died before it marked the intent ready — and the
    other gives a task to an intent that was published and never dispatched. See
    `docs/research/2026-09-17-a-publication-that-stopped-half-way-is-finished.md`.
    """
    try:
        result = sweep(queue, coordinator, state_root=Path(STATE_ROOT))
    except Exception as error:  # noqa: BLE001 - recovery must not break the worker
        _count_dropped_capture("capture_adoption", error, None)
        return
    _record_adoption_skips(result.get("skipped") or [])


def _record_adoption_skips(skipped: Sequence[Mapping[str, object]]) -> None:
    """An intent the pass could not adopt is a standing loss: say so, once per pass.

    The result used to be thrown away, so an intent that could never be adopted was
    re-read on every pass and named nowhere. See
    `docs/research/2026-09-17-the-adoption-pass-says-what-it-skipped-and-looks-past-it.md`.
    """
    from capture_diagnostics import record_capture_failure

    standing = [skip for skip in skipped if not skip.get("retried")]
    if not standing:
        return
    first = standing[0]
    record_capture_failure(
        "capture_adoption",
        f"{len(standing)} intent(s) not adopted; first {first.get('intent_id')}: {first.get('reason')}",
    )


def run_capture_worker_once(
    queue: object,
    coordinator: object,
    *,
    process_missing: Callable[[object, object, object, object, object], object],
) -> object | None:
    # An intent with no task is invisible to `recover_expired_leases`, which
    # recovers a task whose lease expired and so presupposes a task. See
    # `docs/research/2026-08-28-adopting-an-orphaned-intent.md`.
    _adopt_orphaned_intents(queue, coordinator)
    registry = queue.ownership_registry()
    scope = "worker:capture-recovery"
    owner = registry.acquire("queue-worker", scope=scope)
    try:
        with queue.queue_owner(role="queue-worker", scope=scope, parent=owner):
            # Reclaim first: `claim_capture` selects `state='ready'` only, and on
            # the adopted V3 runtime nothing else sweeps — doctor's recovery still
            # walks the retired file queue. Without this a capture whose worker
            # died stays leased forever and the session is lost in silence, which
            # is what stranded two of them here on 2026-08-26.
            queue.recover_expired_leases()
            lease = queue.claim_capture("capture-worker")
            if lease is None:
                return None
            return _process_or_fail(
                queue, coordinator, lease, owner, process_missing
            )
    finally:
        registry.release(owner)


def _process_or_fail(
    queue: object,
    coordinator: object,
    lease: object,
    owner: object,
    process_missing: Callable[..., object],
) -> object:
    """Settle the claim either way: a failure is a named retry, not a stuck lease.

    Measured on this vault on 2026-08-27: a provider timeout raised out of the
    worker, the CLI boundary swallowed it with exit 0, and the task sat leased
    until its TTL expired — three silent attempts before anyone could see why.
    """
    try:
        return process_capture_lease(
            queue,
            coordinator,
            lease,
            owner=owner,
            process_missing=process_missing,
        )
    except Exception as error:
        with contextlib.suppress(Exception):
            queue.fail(lease, _capture_queue_failure(error))
        _raise_if_attempts_spent(lease, error)
        raise


def _capture_queue_failure(error: BaseException) -> object:
    """What the queue is told: an absent provider states its wait, the rest do not.

    The backoff is the queue's own. A stated wait of zero said nothing, whatever
    the comment that used to stand here claimed.
    """
    from memory_queue import QueueFailure

    if isinstance(error, CaptureProviderUnavailable):
        return QueueFailure("provider_unavailable", retry_after=PROVIDER_RETRY_SECONDS)
    return QueueFailure("processor_failed")


def _raise_if_attempts_spent(lease: object, error: BaseException) -> None:
    """The last attempt's failure is a loss, and is raised as one so it is recorded as one."""
    from capture_diagnostics import DurableWorkExhausted
    from reliable_memory import DEFAULTS

    if int(getattr(lease, "attempt", 0)) >= DEFAULTS.queue_max_attempts:
        raise DurableWorkExhausted("capture task spent its last attempt") from error


def _capture_feedback(tier: str, body: str, args: argparse.Namespace) -> None:
    if tier not in {"major", "minor"}:
        return
    if not body:
        return
    try:
        from feedback_capture import capture_from_text

        capture_from_text(
            body,
            session_id=args.session_id,
            slug="unknown",
            trigger=args.event,
        )
    except Exception as exc:  # noqa: BLE001 - counted, never silent (audit OPS-21)
        _count_dropped_capture("feedback_capture", exc, args.session_id)


def _count_dropped_capture(kind: str, error: BaseException, session_id: str | None) -> None:
    from capture_diagnostics import record_capture_failure
    from secret_redact import describe_error

    record_capture_failure(kind, describe_error(error), error=error, session_id=session_id)


def _record_empty_state(state: dict, args: argparse.Namespace) -> None:
    record_flush(state, args.session_id, args.event)
    state["flush_empty_count"] = int(state.get("flush_empty_count", 0)) + 1
    state["last_flush_empty_at"] = datetime.now().isoformat(timespec="seconds")
    counts = state.setdefault("flush_tier_counts", {})
    counts["ok"] = int(counts.get("ok", 0)) + 1


def _record_empty_flush(args: argparse.Namespace) -> None:
    update_state(lambda state: _record_empty_state(state, args))


def _flush_body(body: str) -> str:
    if body:
        return body + "\n"
    return "(tier flagged but no structured body - manual review needed)\n"


def _flush_block(
    args: argparse.Namespace, tier: str, body: str, now: datetime
) -> str:
    header = f"\n## [{now.strftime('%H:%M:%S')}] {args.event} | {args.session_id}\n"
    meta = (
        f"- Trigger: `{args.trigger}`\n"
        f"- Agent: `{getattr(args, 'agent', 'unknown')}`\n"
        f"- Transcript: `{args.transcript}`\n"
        f"- Tier: `{tier}`\n"
    )
    return redact_secrets(header + meta + "\n" + _flush_body(body))


def _flush_operation_id(args: argparse.Namespace) -> str | None:
    source_event_id = getattr(args, "source_event_id", "")
    if not source_event_id:
        return None
    return f"flush:{source_event_id}"


def _claim_flush(state: dict, args: argparse.Namespace, claimed: list[bool]) -> None:
    """Record the flush now, so a concurrent one of the same session and event skips."""
    if should_skip(state, args.session_id, args.event):
        return
    record_flush(state, args.session_id, args.event)
    claimed.append(True)


def _release_flush_claim(state: dict, args: argparse.Namespace) -> None:
    state.get("flush_dedupe", {}).pop(dedupe_key(args.session_id, args.event), None)


def _count_flush_tier(state: dict, tier: str) -> None:
    counts = state.setdefault("flush_tier_counts", {})
    counts[tier] = int(counts.get(tier, 0)) + 1


def _persist_flush(
    args: argparse.Namespace, tier: str, block: str, day: str
) -> list[tuple[Path, str]]:
    """Claim under the state lock, append outside it, then count.

    The append used to run inside the lock, so a busy Markdown writer held every hook's
    0.1 s state lock. See `docs/research/2026-09-14-no-markdown-write-under-the-state-lock.md`.
    """
    claimed: list[bool] = []
    update_state(lambda state: _claim_flush(state, args, claimed))
    if not claimed:
        return []
    daily_path = _append_claimed_flush(args, day, block)
    update_state(lambda state: _count_flush_tier(state, tier))
    return [(daily_path, tier)] if tier == "major" else []


def _append_claimed_flush(args: argparse.Namespace, day: str, block: str) -> Path:
    try:
        return append_daily(day, block, operation_id=_flush_operation_id(args))
    except BaseException:
        update_state(lambda state: _release_flush_claim(state, args))
        raise


def _trigger_deferred_compiles(deferred: list[tuple[Path, str]]) -> None:
    for daily_path, tier in deferred:
        update_state(
            lambda state, path=daily_path, flush_tier=tier: maybe_trigger_compile(
                state, path, flush_tier
            )
        )


def _settle_flush(args: argparse.Namespace, raw_summary: str) -> None:
    """Turn one classified session into whatever it earned, if anything."""
    tier, body = _classify_response(raw_summary)
    _capture_feedback(tier, body, args)
    if tier == "ok":
        _record_empty_flush(args)
        return
    now = datetime.now()
    block = _flush_block(args, tier, body, now)
    deferred = _persist_flush(args, tier, block, now.strftime("%Y-%m-%d"))
    _trigger_deferred_compiles(deferred)


def _run_flush(args: argparse.Namespace) -> int:
    if should_skip(load_state(), args.session_id, args.event):
        return 0
    _keep_transcript_record(args)
    raw_summary = _flush_summary(args)
    if raw_summary is not None:
        _settle_flush(args, raw_summary)
    return 0


def main() -> int:
    args = parse_args()
    completed = False
    try:
        result = _run_flush(args)
        completed = result == 0
        return result
    finally:
        if completed and args.ephemeral_transcript and args.transcript:
            _cleanup_ephemeral_transcript(args.transcript)


if __name__ == "__main__":
    raise SystemExit(main())
