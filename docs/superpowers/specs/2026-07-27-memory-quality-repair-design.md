# LLM Wiki Memory Quality Repair Design

## Goal

Make the installed memory pipeline retain user intent and reusable knowledge
instead of agent telemetry, while preserving existing data and keeping model
usage, latency, and maintenance cost bounded.

## Constraints

- Keep the existing three-zone layout, paths, environment contracts, and
  OpenCode runtime location.
- Do not rewrite append-only daily logs or automatically merge/delete notes.
- Add no runtime dependency and no additional model call.
- Keep `openai/gpt-5.6-luna` as the only OpenCode service model.
- Preserve queue FIFO order, leases, retries, digest checks, and crash recovery.
- Prefer deterministic rejection before model-based judgment.
- Keep old heading daily records and current compact bullet records readable.

## Approaches Considered

### 1. Expand deny lists

Add more command names to the current probe filters. This is fast but fails
open: every new read-only command becomes noise until explicitly listed, and
the JavaScript/Python implementations will continue to drift.

### 2. Boundary gates with additive compatibility (selected)

Classify events by stable meaning rather than command spelling, preserve event
provenance through deferred work, make maintenance re-triggerable, parse both
daily grammars, and enforce durable-knowledge rules before write. This closes
the confirmed defects without a data migration or extra model cost.

### 3. Replace Markdown events and JSON queue with one new event store

A versioned structured event store would simplify future analytics, but it
would change persistence contracts, require migration and rollback machinery,
and create avoidable risk for the installed vault. It is not justified for the
current scale.

## Design

### 1. Capture Boundary

OpenCode will use its documented `worktree` as the default project identity and
event-local tool paths/work directories when available. A target resolved
inside the memory vault is suppressed even when OpenCode itself was started in
the vault's parent directory.

Tool breadcrumbs are diagnostic records, not knowledge. Only direct file
mutation tools with a concrete path are retained. Shell, search, read, status,
and inspection tools are discarded because their command text does not contain
a reliable outcome and cannot justify prompt/context cost. The Python helper
keeps the same fail-closed policy as a defensive boundary.

Idle classification receives only user and assistant conversational text, not
system messages, reasoning, or tool output. Its prompt uses the same durable
criteria as `flush_memory.py`: task status, audit verdicts, file listings, and
code-derived facts return `FLUSH_OK`.

### 2. Event Provenance

Every deferred flush carries the source session, project slug/root, trigger,
and occurrence time. Immediate and deferred apply paths render the same fields.
The queue envelope remains an extensible JSON object, so this is backward
compatible: legacy tasks without provenance still apply with explicit
`unknown`/current-time fallbacks.

The design follows the stable event-data convention of separating occurrence
time, event type, source/resource, attributes, and body. It does not adopt a
new wire format or dependency.

### 3. Maintenance State Machine

Replace the plugin-lifetime `started` latch with three states:

- `running`: prevents overlapping consumers;
- `requested`: coalesces a trigger received during a run;
- `continuationScheduled`: guarantees one bounded follow-up after the five-task
  limit.

Session creation and system-context transformation may request maintenance.
One run processes at most five tasks for responsiveness. Reaching the limit or
receiving another request schedules exactly one follow-up. A task is still
acknowledged only after successful idempotent apply; failures remain durable
and respect retry timing.

### 4. Daily Read Model And Session Context

Daily files remain append-only. The reader normalizes legacy heading blocks and
compact prompt/tool bullets into one in-memory record shape. Selection uses
source order, project provenance, and record quality:

1. current-project durable summary;
2. current-project user prompt;
3. legacy unscoped durable summary;
4. no excerpt.

Tool breadcrumbs are never injected into the model's session context. If the
newest date has no eligible record, the reader checks older dates within a
small fixed window.

Section clipping preserves complete lines and one truncation marker. Unused
reserved space is redistributed to project state and the latest useful daily
record without exceeding the existing global character limit. Integrations
that separately inject full project state can request its omission from the
combined context; OpenCode keeps it enabled.

### 5. Durable Knowledge Admission

Provider output is untrusted proposed data. Before a compile plan is journaled
or applied, Python enforces:

- every citation belongs to a recognized durable section such as decisions,
  lessons/patterns, gotchas/debugging, or reusable commands;
- plain status/audit/code-summary blocks cannot create notes;
- create targets must not already exist; update targets must exist;
- create bodies obey the documented word bounds;
- normalized slug, title, and one-sentence summary do not match an active note
  or an earlier create in the same plan.

Similarity remains deliberately conservative and deterministic. Ambiguous
semantic pairs are reported for review rather than merged or deleted. Provider
audit counters remain diagnostics and cannot bypass these checks.

### 6. Retrieval And Existing Data

No existing note is automatically changed. Superseded history remains
available and excluded by existing retrieval rules. Repair audit continues to
report duplicate candidates only. Prevention stops the corpus from degrading
further; existing semantic pairs require a separately reviewed canonicalization
manifest.

## Failure Handling

- Missing event-local project data falls back to the documented OpenCode
  worktree, then directory; it never falls back to another project's heartbeat.
- Missing legacy provenance is explicit and does not block old queued tasks.
- A failed queue task remains pending with its existing attempt/backoff rules.
- A rejected compile plan performs no note mutation and leaves the source batch
  pending with a durable reason.
- Context parsing treats malformed daily lines as untrusted text and skips them.

## Verification

Every behavior starts with a regression test that fails against the current
implementation. Focused suites cover capture, queue orchestration, provenance,
daily compatibility, project isolation, bounded context, and compile admission.
Completion also requires Ruff, the full pytest suite, cross-platform syntax
checks, memory lint, plugin target hash equality, and a controlled live OpenCode
smoke test. No production cleanup apply is part of this repair.

## Current Practice References (checked 2026-07-27)

- OpenCode plugin events and `directory`/`worktree` context:
  https://opencode.ai/docs/plugins/
- OpenCode session lifecycle and server endpoints:
  https://opencode.ai/docs/server/
- OpenTelemetry stable log/event data model (timestamp, body, resource,
  attributes, event name):
  https://opentelemetry.io/docs/specs/otel/logs/data-model/
- CloudEvents source/id/time/type separation and compact-event guidance:
  https://github.com/cloudevents/spec/blob/main/cloudevents/spec.md
- Microsoft competing-consumer guidance: bounded workers, leases, idempotency,
  poison-message retention, and result correlation:
  https://learn.microsoft.com/en-us/azure/architecture/patterns/competing-consumers
- AWS reliability guidance for idempotency tokens, durable state, retries, and
  behavioral tests:
  https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/rel_prevent_interaction_failure_idempotent.html
- OWASP guidance to separate instructions from untrusted data and validate model
  output deterministically, especially for persistent/RAG poisoning:
  https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html
- Anthropic retrieval guidance on hybrid exact/semantic retrieval,
  deduplication, bounded top-k context, and evaluation:
  https://www.anthropic.com/news/contextual-retrieval
