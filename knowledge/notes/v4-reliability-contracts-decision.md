---
type: decision
title: "V4 Reliability Uses Versioned Input Identity And Durable Operational Ownership"
description: "V4 reliability repair adds path-bound compile receipts, truthful queue serialization, durable capture intents, unified fenced ownership, and bounded execution while preserving Markdown authority and the 12-tool local runtime."
date: 2026-08-05
confidence: high
source_authority: user
status: active
---
# V4 Reliability Uses Versioned Input Identity And Durable Operational Ownership

One-sentence summary: V4 reliability repair adds path-bound compile receipts, truthful queue serialization, durable capture intents, unified fenced ownership, and bounded execution while preserving Markdown authority and the 12-tool local runtime.

## Decision

Date: 2026-08-05.

The user approved the repair direction and implementation base, then explicitly
delegated the exact architecture decisions after current-source review. LLM Wiki will
repair the current production boundaries in layers rather than patching individual
failing assertions or replacing the runtime. The approved changes are:

- `compile-receipt/v3`, keyed by logical source path plus content digest and bound to
  a complete per-source batch disposition;
- `queue-task/v3`, matching production serialization and enforcing payload hash plus
  exact dedupe identity before work is dispatched, with nonterminal semantic decisions
  separate from the one terminal result slot;
- create-only `capture-intent/v1` records under `run/capture-intents/`, retained until
  an immutable terminal record proves committed Markdown, validated no-durable-content,
  or explicit operator discard; queue enqueue alone is never terminal ownership;
- one canonical fenced admission protocol for queue workers, compilers, Doctor,
  capture, project/Markdown writers, nightly, weekly, and LSP, using two versioned
  operational SQLite replacements in the same `run/` location;
- an explicit offline ownership-v3 adoption step that replaces legacy active database
  paths with verified JSON tombstones, so known v2 queue/transaction clients cannot
  reach v3 state;
- atomic, restartable SQLite migrations that verify completed invariants;
- one absolute deadline and bounded I/O/work/output contract across providers,
  subprocesses, graph queries, and MCP responses;
- installer and CI contracts that are fail-closed, XDG-aware, locked, no-sync during
  unattended runtime, and verified on Python 3.10 and 3.14.

The path and environment delta is limited to `run/capture-intents/`, semantic-decision,
terminal, and corrupt-task disposition records under existing `run/queue-results/`,
malformed capture packages under existing `run/queue-quarantine/`,
transaction-abort receipts under existing `run/transactions/<transaction-id>/`,
path-bound receipts under existing
`knowledge/daily/receipts/`, active `run/markdown-transactions-v3.sqlite3` and
`run/queue-v3.sqlite3`, retained `*-v2-retired.sqlite3` files on upgrade, JSON
tombstones at the two legacy database paths, `run/reliability-v3-migration.json`,
`run/reliability-v3-adopted.json`,
`scripts/repair_installed_memory.py`, v3 schema files, and remote-bootstrap input
`LLM_WIKI_COMMIT`. Changed MCP data payloads are versioned; the 12 tool names and
outer envelope remain unchanged.

Markdown, Git, project journals, live source bytes, accepted decisions, and accepted
artifacts remain authoritative. Operational SQLite remains coordination state in
rollback-journal mode with `synchronous=FULL`. The repair adds no daemon, remote
queue, automatic Git operation, new MCP tool, graph authority, or runtime root.

## Rationale

The 2026-08-05 audit found that strong subsystem primitives were undermined at their
boundaries. Identical source bytes at different logical paths shared compile
authority; queue hashes were stored but not enforced; live workers were not always
visible to the deletion contract; provider and detached-process failures could be
counted as semantic OK; and model, graph, installer, and MCP work could exceed the
budget advertised to callers.

Versioned identities and durable ownership address those causes without rewriting
the working transaction, queue, retrieval, or LSP foundations. Keeping the current
three-zone and authority model limits migration risk for installed vaults.

## Consequences

- New capture intent files are operational state under `run/`, not disposable cache.
- Existing transient transcript files remain recovery input until safely transferred.
- Queue ownership of a task never permits deletion of an unresolved capture intent.
- Provider-derived decisions are immutable before side effects and are reused during
  crash recovery.
- Per-intent and per-task fences linearize worker commits against operator discard or
  corrupt-task quarantine.
- Semantic-decision records have at least the existing 30-day retention. Compact
  terminal records survive ordinary purge and suppress replay until explicit
  whole-`run/` deletion, which deliberately forfeits that old-event authority.
- V2 queue schemas and compile receipts remain historical and readable but do not
  authorize automatic v3 skip or archive.
- Legacy PID and maintenance lock files remain compatibility deletion blockers during
  migration.
- Ownership v3 is never auto-adopted in an installed vault; incomplete adoption keeps
  v3 mutation disabled and requires the vault to remain offline until explicit repair
  finishes.
- Retired v2 database bytes remain deletion blockers in this repair. Complete
  tombstone/adoption evidence does not independently block otherwise eligible
  whole-`run/` deletion; the active operational database count remains two.
- A source receives no completed compile receipt unless its disposition is validated.
- A queue dedupe collision with different work is an error, not silent aliasing.
- Unknown live owner roles block `run/` deletion.
- Runtime and installer hot paths do not update the lockfile or environment.
- Public install URLs remain unpublished until an immutable release exists.

## Rejected Alternatives

- Minimal test-only patches were rejected because they leave data-loss and authority
  defects in place.
- A runtime rewrite was rejected because existing primitives can be reused safely.
- A thirteenth MCP checkpoint tool was rejected because it is unnecessary for this
  reliability repair and conflicts with the approved 12-tool boundary.
- SQLite WAL was rejected because it adds no required capability to this local
  coordination workload.

## Source / Evidence

- User approval of the repair direction, implementation base, and delegated exact
  architecture decisions in the 2026-08-05 OpenCode session.
- `docs/superpowers/specs/2026-08-05-v4-reliability-repair-design.md`
- OpenCode 1.18.13: https://github.com/anomalyco/opencode/releases/tag/v1.18.13
- Python 3.14.6 filesystem contract: https://docs.python.org/3.14/library/os.html
- SQLite transaction control: https://docs.python.org/3.14/library/sqlite3.html
- uv locking and syncing: https://docs.astral.sh/uv/concepts/projects/sync/
- XDG Base Directory Specification 0.8: https://specifications.freedesktop.org/basedir-spec/latest/
