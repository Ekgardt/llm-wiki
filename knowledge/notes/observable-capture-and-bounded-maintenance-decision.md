---
type: decision
status: accepted
date: 2026-08-18
---

# Observable capture and bounded maintenance output

One-sentence summary: A failed capture is recorded durably instead of vanishing,
maintenance step output streams to owner-only artifacts under bounded retention,
and the SessionStart payload has a hard character ceiling.

## What changed

Three silent-loss paths closed, all of them fail-open by design and none of them
observable before this change.

**Capture failures leave a trace.** `scripts/capture_diagnostics.py` records every
lost prompt or post-tool capture in two places: one bounded JSONL trail at
`logs/capture-failures.jsonl` carrying the redacted reason, and one counter per
failure kind in `state.json`. `user_prompt_capture.py` and `post_tool_capture.py`
call it from their append path and from their last-resort handler. Both hooks
still exit 0 — recording never becomes the reason a hook fails. The SessionStart
metacognitive block names the loss, so it reaches the agent and the user rather
than only a log file.

**Maintenance step output is bounded and pointed at.** `run_step` no longer reads
a child process's stdout and stderr into memory. Both streams go straight to
owner-only (`0o600`) artifacts under `logs/maintenance/`, the report keeps the
same short summary — the first 300 characters of stderr on failure, the last six
lines of stdout — and every step line now names the artifact holding the full
output. `prune_reports` applies age (30 days), count (60 files), and total size
(32 MiB) limits over each report family, and the nightly pass prunes
`nightly-*.md`, `weekly-*.md`, `lint-*.md`, and the artifact directory instead of
only its own reports.

**The injected SessionStart payload has a ceiling.** The shared context budget is
counted in tokens, which cannot bound characters, so the injected block grew with
every new decision page and log entry. `SESSION_CONTEXT_MAX_CHARS` (4000) is now
the hard ceiling: whole sections are dropped lowest priority first, mandatory
sections are never dropped, and no section is ever sliced. Both injection paths —
the direct hook and the integration adapter — go through the same function.

## Why

`OPEN-013` and `OPEN-040` in the developer audit describe the same failure shape:
a subsystem chose to keep running rather than break a session, and then made the
choice invisible. A capture that fails without a record cannot be distinguished
from a session with nothing to capture; a truncated report line with no pointer
cannot be distinguished from a step that produced nothing more. Both defects hide
data loss behind a design decision that was correct on its own.

The nightly pass also had no answer for its own growth: it pruned its own reports
after 30 days and nothing else, so weekly reports, lint reports, and any future
artifact grew without limit on an unattended machine.

`NEW-02` is the same class one level up: the SessionStart block is injected into
every session, so unbounded growth there spends the user's context on the memory
system describing itself. The failing `tests/test_context_noise.py` case was the
only thing making that visible, and it had already gone red at 4167 characters.

## What this gives up

Retention deletes evidence. A failure investigated more than 30 days later, or
after 60 further maintenance runs, will find its artifact gone; the summary line
in the report survives, the full output does not. That is the accepted trade for
a bounded disk on an unattended machine.

The character ceiling drops whole sections when the payload is over budget. On a
vault with many guard rails and a long log tail, the index, daily, and log
sections leave the injected block entirely — visible, deterministic, and by
priority, but they do leave.

Capture diagnostics record that a capture was lost and why, not what was lost.
The content of the failed capture is gone; only its kind, timestamp, project, and
redacted error reach the trail.

## Alternatives considered

* **Retry the failed capture.** Rejected here: retrying inside a lifecycle hook
  adds latency to the user's session for an event whose payload may itself be the
  reason for the failure. The durable capture-intent path (`SessionEnd`,
  `PreCompact`) already provides the recovery-based answer; prompt and post-tool
  capture stay best-effort and now stay visible.
* **Route capture failures into `doctor`.** The right long-term surface, and
  deferred: `scripts/doctor.py` carries 134 complexity-gate findings, so touching
  it means refactoring a 4953-line file. The counter in `state.json` is already
  readable by any surface that wants it, including a later doctor check.
* **A single merged artifact per step.** Rejected: the report distinguishes
  stderr (head, on failure) from stdout (tail), and merging the streams would
  make that summary depend on interleaving order.

## Source / Evidence

- `scripts/capture_diagnostics.py` — the durable trail and the state counter.
- `scripts/user_prompt_capture.py`, `scripts/post_tool_capture.py` — the two
  call sites where a capture used to disappear.
- `scripts/maintenance_helpers.py` — streaming `run_step` and `prune_reports`.
- `scripts/scheduled_nightly.py` — retention applied to every report family.
- `scripts/session_start_context.py` — `SESSION_CONTEXT_MAX_CHARS` and
  `fit_to_char_ceiling`; `scripts/integration_adapter.py` uses the same function.
- `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md` — `OPEN-013`, `OPEN-040`, `NEW-02`.
- `tests/test_capture_diagnostics.py`, `tests/test_maintenance_helpers.py`,
  `tests/test_context_noise.py`.

## Related

- [[knowledge/notes/durable-capture-producer-activation-decision]]
- [[knowledge/notes/audit-closure-security-recovery-control-plane-decision]]
- [[knowledge/notes/v4-reliability-contracts-decision]]
- [[knowledge/notes/reliable-memory-stage-2]]
- [[knowledge/notes/system-symlink-ancestor-decision]]
- [[knowledge/notes/self-resolving-health-findings-decision]] — what bounds the finding this decision made visible, so it can end.
- [[knowledge/notes/session-evidence-retention-decision]] — links to this page.
- [[knowledge/notes/bounded-capture-excerpt-decision]] — a transcript larger than
  the evidence bound is excerpted at both ends rather than refused.
- [[knowledge/notes/identity-is-a-function-of-its-content-decision]] — links to this page.
