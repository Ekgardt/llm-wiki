---
type: decision
status: active
confidence: high
source_authority: user
date: 2026-09-02
---

# The Memory Keeps A Second Copy, And The Undo Trail Stops Pretending To Be One

One-sentence summary: settled transactions drop their images after two days
instead of thirty, and the memory gains a real second copy with history at
`~/llm-wiki-snapshots/`, outside the vault and with no remote.

## Context

Measured on this vault 2026-09-02, and the numbers are the argument.

`run/` had reached **5.0 GB**, 11 369 transactions, median 301 KB each. Every
write stored a full copy of the file before and a full copy after: one
transaction spent 444 KB to add 1 015 bytes to a journal. Half of all images
were byte-identical duplicates, and 79% of what remained was compressible.

Two causes, not one. The retention window was thirty days — and the number lived
as a literal in four separate files and twice inside one of them. And nothing
ever called `prune`: the machinery existed, was correct, and had never run.

Meanwhile the thing actually worth protecting had **no copy at all**.
`knowledge/` is 20 MB; of 116 pages 83 are tracked in git and of 15 daily logs
only 3, because the rest is private by design in a public repository. The undo
trail was not a backup of it — it held per-write images, not restorable states —
so the private memory existed in exactly one place on one disk.

## What the field does

Crash consistency is transient everywhere. Oracle's `UNDO_RETENTION` is a
minimum expressed in seconds and its rollback segments are explicitly reusable
by later transactions; PostgreSQL leaves old row versions in the table and
autovacuum removes them as soon as no transaction needs them. The rule above
both is that a log record for an active transaction may not be discarded until
it commits or rolls back — and after that, it may.

Restoring an earlier state is a different job with a different answer: a backup.
PostgreSQL's own documentation says that once a transaction commits, rollback
cannot undo it, and points at point-in-time recovery from a base backup plus
archived log. The practice around backups is 3-2-1, still cited in 2026 by
CISA's ransomware guide and NIST SP 800-209 as the minimum acceptable
architecture.

We were doing both jobs with one artefact, badly.

## Decision

**The undo window is two days, and it is one number in one place.**
`markdown_transaction.UNDO_RETENTION_DAYS`; `doctor`, `archive_daily` and
`installed_memory_repair` import it. Two rather than zero so a crash spanning
midnight can still be unwound before the next snapshot exists, and because a
floor of one makes "shorter than the window" indistinguishable from "not a valid
number of days".

**The nightly pass prunes.** `reclaim_runtime_state.prune_settled_transactions`
runs it through `active_or_legacy_coordinator`, never by constructing a
coordinator directly — adoption replaces the pre-adoption database with a
tombstone, and a writer that opens that path dies on it.

**The memory keeps a second copy** at `~/llm-wiki-snapshots/`, taken by the same
nightly pass. Outside the vault, so wiping the vault does not take it along;
owner-only; and its repository has **no remote and never gets one**, which is
the whole answer to whether it can leak.

**Git rather than Restic**, deviating from the research note that recommended
Restic. Restic's advantage is client-side encryption, which matters when a copy
leaves the machine — and the owner ruled out any cloud destination. What remains
is history, deduplication and compression of a small text tree, which git
already does with no new dependency, no daemon, and no passphrase whose loss
would destroy the backup.

## What was traded away, explicitly

Point-undo of any committed write within a month. The owner accepted this on
2026-09-02 in exchange for a daily snapshot: recovery becomes "restore
yesterday's state" rather than "reverse that one write from three weeks ago".

## Consequences

`run/` went from 5.0 GB to **307 MB** the moment pruning first ran — 11 171
artefacts removed. The snapshot repository is 29 MB for the whole memory with
its history.

The copy is on the same disk, at the owner's request. That protects against a
bad write, a bad compile and a mistaken deletion; it does **not** protect
against the disk failing. 3-2-1 asks for one copy elsewhere and we have none,
and this decision does not close that.

Snapshots are never pruned, because a text tree's history is small enough not to
need it and because pruning history is how the last version of something quietly
disappears.

## Source / Evidence

- `docs/research/2026-09-02-where-undo-belongs-and-for-how-long.md`
- `docs/research/2026-09-02-what-belongs-in-the-repository-and-what-the-undo-trail-should-be.md`
- `scripts/markdown_transaction.py` — `UNDO_RETENTION_DAYS`, `_prune_cutoff`
- `scripts/reclaim_runtime_state.py` — `prune_settled_transactions`,
  `snapshot_memory`
- `scripts/snapshot_knowledge.py`
- `tests/test_the_memory_keeps_a_second_copy.py`,
  `tests/test_runtime_state_is_reclaimed.py`
- Measurement: `run/transactions` 5.0 GB → 307 MB, 11 171 pruned, 2026-09-02

## Links

- [[knowledge/notes/reliable-memory-stage-2]] — the transaction contract this
  narrows.
- [[knowledge/notes/single-directory-vault-decision]] — why the vault and the
  public source share a directory, and why the copy must not.
- [[knowledge/notes/session-evidence-retention-decision]] — what the private
  memory contains that git does not hold.
- [[knowledge/notes/observable-capture-and-bounded-maintenance-decision]]
