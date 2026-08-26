---
type: decision
status: active
confidence: high
source_authority: ai-derived
date: 2026-08-26
---

# A Transcript Too Large to Keep Whole Is Excerpted, Never Refused

One-sentence summary: when a capture transcript is larger than the durable
evidence bound, the adapter keeps its beginning and its end and says in the
evidence itself how many bytes it dropped — refusing the whole session was
losing exactly the sessions the bound existed to protect.

## The problem, measured

`pre_compact` fires because a conversation got long. The capture path read the
transcript with `read_stable_utf8(path, MAX_CAPTURE_EVIDENCE_BYTES)`, which
refuses any file over 900 KiB, so the hook that only ever sees long sessions
refused them. On the live vault on 2026-08-26 `run/state.json` held
`adapter_pre_compact: ValueError: capture transcript exceeds 921600 bytes` —
one lost session, and every future one of that size.

Measured on this machine the same day, across the 487 host transcripts under
`~/.claude/projects`: median 144 KB, p90 678 KB, **36 over the bound**, largest
**105 MB**. That measurement is what rules out the obvious repair. Raising the
ceiling would have to reach 105 MB, and a lifecycle hook cannot hold 105 MB in
memory to keep 900 KiB of it.

## What was decided

The bound stays; the refusal goes. Over the bound, the adapter reads one window
from each end of the file — `CAPTURE_EXCERPT_SIDE_BYTES`, half the evidence
budget each — trims each to whole lines where the window holds a newline, and
joins them with a marker naming the dropped byte count. Under the bound nothing
changes: the transcript is still read whole through the same stable reader.

Head **and** tail, not either alone. That is the same choice already recorded
for nightly consolidation, for the same measured reason: a long session states
its decisions early and its outcome late, and a tail-only window was shown to
miss decisions sitting 31,814 characters from the end.

## Why truncating costs nothing the bound was protecting

The durable record this evidence feeds is already capped and already truncates:
`session_evidence.MAX_EVIDENCE_BYTES` keeps 512 KiB and appends its own
`_(record truncated at the size limit)_` note. So for every transcript above
512 KiB the vault was going to keep a truncated record anyway. Refusing the
whole session to protect a record that would have been truncated is the wrong
trade — it converts a partial record into no record.

Measured against the 1 MiB `MAX_CAPTURE_INTENT_BYTES` bound the excerpt must fit
inside: a 900 KiB excerpt of a real 41 MB transcript encodes to 944,343 bytes,
inside the bound. That headroom is about 8%, and it is content-dependent —
JSON escaping of a pathological transcript could still exceed it and raise
`capture intent exceeds its byte limit`. That risk is unchanged by this
decision: it already applied to every 900 KiB transcript the old code accepted.

## What this does not fix

Nothing here adopts Reliability V3. On a vault that has not adopted it, capture
still stops at `ReliabilityV3ValidationError: legacy_protocol_unquiesced`, one
layer behind the size check. Measured: with the size refusal removed, the same
real 922,240-byte transcript moved from
`capture transcript exceeds 921600 bytes` to exactly that blocker. That blocker
is a separate, already-recorded matter and was deliberately not run.

## Source / Evidence

- `scripts/integration_adapter.py` — `_capture_transcript_text`,
  `_capture_excerpt_text`, `_read_transcript_edges`, `CAPTURE_EXCERPT_SIDE_BYTES`.
- `scripts/session_evidence.py` — `MAX_EVIDENCE_BYTES`, `TRUNCATION_NOTE`: the
  512 KiB the record keeps regardless.
- `tests/test_plugin_helpers.py` — the excerpt is bounded, keeps both ends, and
  counts what it dropped; a short transcript is still returned whole.
- Live vault `run/state.json`, 2026-08-26: `adapter_pre_compact` held the
  refusal this page removes.

## Related
- [[knowledge/notes/session-evidence-retention-decision]] — the record this
  evidence becomes, and the 512 KiB cap that makes truncation the status quo.
- [[knowledge/notes/observable-capture-and-bounded-maintenance-decision]] — the
  bounded capture path and the failure counter that made this visible.
- [[knowledge/notes/oversized-daily-compile-decision]] — the same shape one
  stage later: an oversized input is divided rather than refused.
