# Capture work keeps its typed handler

Public source base: [`c8dda9abf6f06e17c3a8143bb1ea16c2fe872588`](https://github.com/Ekgardt/llm-wiki/tree/c8dda9abf6f06e17c3a8143bb1ea16c2fe872588).
The observations and combined regression run were made on a local vault
checkpoint whose scripts, tests, pyproject.toml and uv.lock are byte-identical
to that public base. This source comparison establishes applicability; it does
not relabel the earlier execution as a separate run on public main.

Reviewed 2026-09-09. The owner authorized fixing automatic capture dispatch after
internal Codex lifecycle hooks were disabled. This corrects the existing typed
capture contract; it adds no database, environment variable, daemon or CLI flag.

## Observed cause

SessionStart schedules `integration_adapter.py --maintenance`. It ran
`memory_queue.py work`, whose generic processor routes `flush` to `_manual_flush`.
That processor requires payload `prompt`; durable capture payloads instead carry
`intent_id`, `intent_path` and `intent_sha256`. Missing prompt returns false before
any model call, and generic settlement records `processor_failed`. Eight rapid
attempts can therefore kill a valid capture without reading its evidence.

A real user capture reached attempt 8/dead during automatic maintenance. Separately, internal Codex sessions in temporary
`/tmp/llm-wiki-provider-*` directories produced more capture intents. Their event
workers collided with the existing exclusive capture owner (`owner_busy`). These
are two defects, not evidence that Claude classification failed. Timer-only
provider settings did not select the provider for native Codex hooks.

## Sources and choice

Current primary references read before the dispatch change:

- [SQLite SELECT](https://www.sqlite.org/lang_select.html): filter eligible rows
  in WHERE before priority ordering and LIMIT, without modifying rejected rows.
- [SQLite isolation](https://www.sqlite.org/isolation.html): the existing
  BEGIN IMMEDIATE transaction serializes selection and lease mutation.

Inference: exclude rows joined through `capture_task_links` from the generic V3
claim query. Leave the separate `claim_capture` selection and its task/intent
fences, ownership, attempts and immutable terminal proof unchanged. Routing
capture inside `_manual_flush` would be incorrect: that boolean interface lacks
the dedicated lease, owner and completion protocol.

Codebase Memory full index and graph found two production generic-claim callers,
`_claim_when_reachable` and `drain_with`; source inspection confirmed both. Doctor
and CLI work use the former. The only explicit-id `_claim_task` belongs to the
legacy MemoryQueue class; the independent active V3 class has no such method.
`count_eligible` remains an all-ready-work observation, including typed captures.

## Bounded processing and limits

SessionStart runs the existing dedicated `--capture-worker` before generic work.
The dedicated adapter now processes up to 20 successful tasks per process, stopping
between tasks after 450 seconds. This is an admission deadline, not cancellation
of an in-progress model call. The maintenance subprocess keeps its existing
600-second timeout. Each task retains the existing exclusive capture owner and
releases it before the next claim. After a successful limit, one detached successor
checks for remaining work. Empty work stops; a provider or ownership error escapes
to the existing reporting boundary without spawning a retry chain.

This drains finite healthy backlogs and captures arriving while a task is running.
It does not promise unconditional eventual delivery: spawn failure, worker crash,
provider failure, sustained higher-priority work, or a wake racing the final empty
claim/release can still require the next lifecycle/maintenance wake. This patch
does not introduce a persistent scheduler or change retry policy to hide that
limit. Dead tasks are retained and require explicit operator redrive; recursive
captures must not be redriven merely to make health output green.

Regression coverage uses temporary databases and fake processors: generic claims
skip capture while selecting ordinary tasks; dedicated processing produces the
actual immutable terminal; multiple tasks drain; task/time limits schedule one
successor; errors do not spawn a retry chain; maintenance order remains capture,
generic work, compile. Existing terminal/index atomicity and fencing tests now use
the dedicated claim API.
