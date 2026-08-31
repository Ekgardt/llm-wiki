---
type: decision
status: accepted
confidence: high
source_authority: user
created: 2026-08-28
---

# Ownership Actor: The Agent, Not The Account

One-sentence summary: a lease's actor is the agent that takes it — process,
role and scope — because naming the machine account instead made
`actor_id UNIQUE` collapse the whole runtime to one lease at a time, so the
nightly pass locked out the compile, the queue and the capture it runs itself.

## Decision

`ownership_actor_identity(role, scope)` derives the actor from the user, the
process identity, and the `(role, scope)` the lease is for. It is the default
for every `OwnershipRegistry.acquire`, and the two helpers that used to name
the plain account — `acquire_compile_owner`, `acquire_scheduled_owner` — use
it too. `current_actor_identity()` keeps meaning *the user* and stays the
provenance value the queue records.

`maintenance_owners` is unchanged: the schema of an adopted database is not
touched, and no migration exists. `PRIMARY KEY(role, scope)` remains the only
rule that binds. A caller that names its own `actor_id` keeps the old
single-lease rule for that name, so `owner_identity_conflict` is still
reachable and still refuses.

## Why

Measured 2026-08-28 in hermetic vaults on the real v3 schema. While
`nightly/global` was held, every other role refused with
`owner_identity_conflict`: `queue-worker`, `capture`, `markdown-writer`,
`project`, `doctor`, `repair` — and `acquire_compile_owner` with them.
`scheduled_nightly` runs its compile as a subprocess and
`maintenance_helpers.run_step` does not carry an owner, so the pass refused
its own child. Two concurrent sessions of one person collided the same way;
the live counters show it as `post_tool_append` and `adapter_session_end`
capture losses, the most recent at 16:04:32 that day.

The product had already worked around this once without naming the rule:
`doctor._v3_maintenance_actor` appends `#doctor-maintenance` so the pass can
enter the Markdown writer gate, and its comment says the maintenance pass is
"a distinct agent of the same user". This decision makes that the rule instead
of the exception.

The contract this vault declares is "one person managing many agents,
sessions, projects, repositories, branches, and worktrees". One person is one
uid, so an ownership rule keyed on the uid contradicts the product outright.

Current practice agrees: ownership is scoped to a session, connection or
process, and one holder routinely holds many locks — an etcd lease covers many
keys, a ZooKeeper session many ephemeral nodes, and a PostgreSQL session
re-acquiring its own advisory lock always succeeds. That last one also removes
the only defensible reason for the constraint: self-deadlock, which cannot
happen when a holder never waits on itself.

## What is given up, named

A dead owner of another `(role, scope)` is no longer reclaimed as a side
effect of somebody else's refusal, because there is no refusal any more. It is
reclaimed when its own `(role, scope)` is next requested — the same proof, one
step later. Collection becomes lazy, and a dead row now blocks only itself
instead of everything.

## Evidence

Before: holding `nightly/global`, six of six other roles refused, and
`acquire_compile_owner` refused. After: six of six acquire, and the compile
owner is granted. Two writers of different scopes coexist. Mutual exclusion is
unchanged — the same `(role, scope)` twice still answers `owner_busy`, a
second nightly still refuses on its marker, and a named actor holding two
leases still answers `owner_identity_conflict`.

## Source

- `docs/research/2026-08-28-who-is-an-actor-in-a-lock.md`
- `scripts/operational_ownership.py`, `scripts/scheduled_nightly.py`
- `tests/test_ownership_actor_is_the_agent.py`

## Related

- [[knowledge/notes/blackboard-fenced-resource-claims-decision]] — the same
  question one layer up: what a claim is keyed on, and why a holder must be
  able to tell its own rows from a stranger's.
