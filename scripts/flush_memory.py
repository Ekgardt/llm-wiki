"""Classify captured sessions: the capture worker's side of the queue.

The adapter publishes a capture intent and a `flush` task for each session event;
`run_capture_worker_once` claims one, `process_new_capture` asks the provider to
classify the session into one of three tiers, and the result is written under the
intent's fence:
  - FLUSH_MAJOR: decisions, lessons, non-obvious commands worth compiling
  - FLUSH_MINOR: gotchas, debug notes, open questions — kept, not compiled first
  - FLUSH_OK:    status chatter — nothing is written
A provider that does not answer is a stated one-hour wait, and a task that spends
its attempts is recorded as a loss. The session record under
`knowledge/raw/sessions/` is written before classification, whatever the tier.

There is no command line: the one that read a transcript file and queued its own
work when no provider answered was retired on 2026-09-25
(docs/research/2026-09-25-the-flush-command-line-is-retired.md).
"""
from __future__ import annotations

import contextlib
import json
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import (  # noqa: E402
    MAX_CAPTURE_INTENT_BYTES,
    ROOT,
    STATE_ROOT,
)
from secret_redact import redact_secrets  # noqa: E402

DAILY_DIR = ROOT / "knowledge" / "daily"
MAX_TRANSCRIPT_CHARS = 60_000
# What a session record may read from a transcript file; the record itself is
# bounded again after rendering.
MAX_RECORD_CHARS = 4_000_000
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
done — that is status, not memory. Keep names exactly as the transcript
spells them — identifiers, file paths, versions, flags, numbers, commands:
a later search finds the bullet by those, and a bullet that renames or
drops them is lost.

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
    """The one classification prompt, on the evidence the intent carries.

    Until 2026-09-23 the capture path sent three lines that named the tier
    tokens and never said what a tier meant, while the measurement stand
    scored `build_classification_prompt`, which no installed hook reached. One
    prompt now serves both, so the stand measures what the product sends. See
    `docs/research/2026-09-23-the-corpus-labels-itself.md`.
    """
    evidence = _readable_evidence(record["evidence"])
    return build_classification_prompt(
        _bounded_classifier_evidence(evidence), str(record["event"])
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
    refused: a body that is only whitespace here, and a reply whose first
    non-blank line declares no tier (`_declared_tier`; a tier quoted further
    down, from the transcript, must not decide).
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
        # Every claim above is already held, so the expiry is counted from here
        # and not from the moment the thread below first gets to run.
        self._held_since = time.monotonic()
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
            held_since=self._held_since,
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
    result = _answered(caller(_capture_prompt(record), CLASSIFICATION_SYSTEM_PROMPT, 1500))
    if not isinstance(result, LLMResult):
        raise RuntimeError("capture provider did not return a provider result")
    tier, body = _parse_capture_wire_output(result.text)
    return result, tier, body


def _answered(result: object) -> object:
    """A provider that did not answer is waited for, whichever way it said so.

    `call_llm_result` answers None when no provider produced text: failed,
    rate-limited or absent. See
    `docs/research/2026-09-25-a-silent-provider-is-a-wait-not-a-failure.md`.
    """
    if result is None:
        raise CaptureProviderUnavailable("no capture provider answered")
    if (getattr(result, "available", True), getattr(result, "failure_class", None)) != (True, None):
        raise CaptureProviderUnavailable("capture provider call did not succeed")
    return result


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
    if _orphaned_by_another_task(indexed, decision, active):
        return _retire_orphaned_decision(candidate)
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


def _orphaned_by_another_task(indexed: object, decision: Mapping[str, object], active: object) -> bool:
    """A decision file no queue row indexes, written for a task that is not this one.

    A task that died between writing its decision and indexing it left the file
    under the intent's key, so its redrive met "capture decision conflicts with
    its binding" and spent its one second chance (audit 2026-09-26 A-12,
    docs/research/2026-09-26-a-redriven-capture-meets-no-leftovers.md).
    """
    binding = decision.get("processing_binding")
    named = binding.get("task_id") if isinstance(binding, dict) else None
    return indexed is None and named != active.task_id


def _retire_orphaned_decision(candidate: Path) -> None:
    """Nothing indexes the file, so it is derived and unreferenced: the decision is made again."""
    from reliable_memory import fsync_directory

    candidate.unlink(missing_ok=True)
    fsync_directory(candidate.parent)
    return None


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
    """What the queue is told: an absent provider states its wait, the rest their code.

    The backoff is the queue's own. A stated wait of zero said nothing, whatever
    the comment that used to stand here claimed. The code is the one
    `processor_error_code` names, so a dead task says why (audit C3, 2026-09-23).
    """
    from memory_queue import QueueFailure, processor_error_code

    if isinstance(error, CaptureProviderUnavailable):
        return QueueFailure("provider_unavailable", retry_after=PROVIDER_RETRY_SECONDS)
    return QueueFailure(processor_error_code(error))


def _raise_if_attempts_spent(lease: object, error: BaseException) -> None:
    """The last attempt's failure is a loss, and is raised as one so it is recorded as one."""
    from capture_diagnostics import DurableWorkExhausted
    from reliable_memory import DEFAULTS

    if int(getattr(lease, "attempt", 0)) >= DEFAULTS.queue_max_attempts:
        raise DurableWorkExhausted("capture task spent its last attempt") from error


def _count_dropped_capture(kind: str, error: BaseException, session_id: str | None) -> None:
    from capture_diagnostics import record_capture_failure
    from secret_redact import describe_error

    record_capture_failure(kind, describe_error(error), error=error, session_id=session_id)


