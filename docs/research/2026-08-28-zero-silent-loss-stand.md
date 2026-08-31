# Zero silent loss, measured: crash injection across the capture write path

Date: 2026-08-28. Task: MEM-18 (`docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`,
section 12, "Асимметричные ставки"). Status of this note: measurement record;
no product code was changed.

One-sentence summary: 108 SIGKILLed captures across 20 injected boundaries of
the real adapter → queue → worker chain produced zero silent losses — every
kill left the content, a replayable trace, or a durable named failure — and
the same stand found three real, deterministic defects that turn one crashed
process into a permanent, visibly-failing capture outage.

## What was measured

The bet under test: this vault's write path (capture intents → queue → worker
→ markdown transactions → session records) claims fail-closed durability. The
stand makes that a number. One trial is:

1. A fresh, fully adopted Reliability-V3 temp vault (built by
   `repair_installed_vault(adopt_ownership_v3=True)`, the same entry the
   product's own tests use), with `MEMORY_LLM_PROVIDER=fake`.
2. One `session_end` capture driven through real product entry points in
   subprocesses: `integration_adapter.publish_capture_intent_from_payload`
   (the adapter's durable publication — intent files, intent index, capture
   task) and `integration_adapter.main(["--capture-worker"])` (the same
   host-safe boundary the detached worker runs behind).
3. One SIGKILL, delivered by the dying process to itself
   (`os.kill(os.getpid(), SIGKILL)`) at a wrapped product boundary — SIGKILL
   cannot be caught, so every try/except is bypassed exactly as a real crash
   bypasses it. Ten stages × before/after = 20 kill points, covering
   publish-intent, intent-ready, enqueue, whole-publication return, claim,
   record-write, classifier, decision-publish, markdown-commit,
   terminal-publish.
4. The documented recovery, up to 4 rounds: expire leases by moving their
   deadlines into the past (the identical clock-advance pattern the product's
   own queue tests use — timestamps only, never rows), then run the worker
   again (`run_capture_worker_once` itself calls `recover_expired_leases`).
5. A byte-level audit of every durable surface: queue tasks and intents,
   intent files, terminal records (with their `markdown_committed` outputs
   re-hashed against the daily file on disk), the daily block, the session
   record, `capture_failures` traces in `run/state.json`, and quarantined
   markdown transactions.

Outcome taxonomy (mapped to the task's trichotomy): `landed` (a — terminal
verified and the queue settled the task), `content-partial` (content already
durable — session record and/or daily block — lifecycle unfinished; visible),
`pending-visible` (b — intent or task durably visible, replay promised),
`named-failure` (c — durable named trace, no content landed), `source-only`
(kill before the first durable write; only the host transcript still carries
the content), `duplicated` (landed more than once), and `silent-loss` —
content gone with no trace, the only outcome that counts against the
property. The classifier is itself tested to be able to say `silent-loss`
(including for a false-success terminal whose claimed outputs do not verify),
so a zero is not vacuous.

Stand: `benchmark/run_durability.py`, `benchmark/durability_stand.py`,
`benchmark/durability_child.py`; tests: `tests/test_durability_stand.py`.

## The numbers

Run: `--trials 108 --seed 0` (plus 2 clean sentinel trials), 2026-08-28,
Linux, fake provider. All 108 kills were observed (child exit by SIGKILL,
rc −9). Aggregate frozen at `benchmark/durability-baseline-2026-08-28.json`.

| outcome | count | of 110 |
|---|---|---|
| landed | 12 | 2 clean sentinels, publish-return:after ×5, terminal-publish:after ×5 |
| duplicated | 0 | the daily block never appeared twice |
| content-partial | 40 | every kill from record-write:after through terminal-publish:before |
| named-failure | 53 | every kill inside a held ownership row/fence that had no content yet |
| pending-visible | 0 | (every visible-pending trial also carried a named trace) |
| source-only | 5 | publish-return:before — killed before the first durable write |
| **silent-loss** | **0** | **target 0 — the property holds** |

Mean recovery runs when a killed trial landed: 0.5 (publish-return:after
needs exactly one worker run; terminal-publish:after none). The outcome per
kill point is fully deterministic — 5–6 trials per point, and every point
produced the same outcome every time:

| kill point | outcome (× trials) | named reason |
|---|---|---|
| publish-intent before/after | named-failure ×12 | `owner_identity_conflict` (D2) |
| intent-ready before/after | named-failure ×12 | `owner_identity_conflict` (D2) |
| enqueue before/after | named-failure ×12 | `owner_identity_conflict` (D2) |
| publish-return:before | source-only ×5 | — (pre-durability window) |
| publish-return:after | landed ×5 | — (1 recovery run) |
| claim before/after | named-failure ×12 | `UNIQUE queue_ownership` (D3) |
| record-write:before | named-failure ×5 | `FOREIGN KEY` (D1; record not yet written) |
| record-write:after … markdown-commit:after | content-partial ×25 | `FOREIGN KEY` (D1; session record durable) |
| terminal-publish:before | content-partial ×5 | `FOREIGN KEY` (D1; terminal file + block durable, task unsettled) |
| terminal-publish:after | landed ×5 | — (0 recovery runs) |

Reading the table honestly: the write path never lost content silently, and
publication-complete or lifecycle-complete crashes recover cleanly — but *no
kill inside a held fence or ownership row ever recovered to landed*, because
of the three defects below. "Retryable" is a promise the ownership layer
currently cannot keep after a crash.

## Three named defects (found, reproduced, not patched)

The stand's most valuable output. All three turn one killed process into a
permanent capture-processing outage. None is a silent loss — every retry
leaves a named `capture_failures` trace and the content or intent stays
durable and visible — but "pending/retryable" stops being true: no documented
recovery entry clears any of them. `repair_installed_vault` on a wedged vault
reports `ok` with zero actions and the wedge persists. The V3 contract's
"unified fenced admission registry with expiry/reclaim" is exactly the part
that does not hold under process death.

**D1 — an orphaned intent fence makes dead-owner reclaim fail forever.**
`intent_fences` carries a real FOREIGN KEY to `maintenance_owners`
(`scripts/markdown_transaction.py`, table DDL). A worker killed while holding
a worker-mode intent fence (any kill from record-write through
terminal-publish:before) leaves the fence row; fence rows are deleted only by
their exact owner (`release_intent_fence`) and `acquire_intent_fence` refuses
on bare row existence with no expiry or liveness consult
(`markdown_transaction.py:4675`). When the dead worker's ownership lease
expires, the next worker's `OwnershipRegistry.acquire` takes the documented
reclaim path — expired + process provably dead — and its `_delete_row`
(`operational_ownership.py:612`, called at `:721`) fails with
`sqlite3.IntegrityError: FOREIGN KEY constraint failed`, unhandled. Every
subsequent capture-worker start dies at acquire, before claiming anything.
Real-time proof with zero database edits: kill at classifier:before, wait
125 s (the `queue-worker` lease TTL is 120 s), run the worker — the trace
reads `IntegrityError: FOREIGN KEY constraint failed`; inside the TTL the
same retry reads `owner_busy`. Session content at that point: session record
already durable, daily block not yet written — kept, but permanently
unfinished.

**D2 — UNIQUE(actor_id) turns one dead producer into an actor-wide
ownership outage.** `maintenance_owners.actor_id` is UNIQUE, and on POSIX the
actor identity is `posix-uid:<uid>` — one row per user. Reclaim of a dead
owner happens only under the same (role, scope)
(`operational_ownership.py:709-721`); an acquisition under any *other*
role or scope never examines the dead row and its INSERT fails with
`UNIQUE constraint failed: maintenance_owners.actor_id`, surfaced as
`owner_identity_conflict` (`operational_ownership.py:758`). A producer killed
inside the publication fence (any kill from publish-intent through enqueue)
holds role `capture`, scope `intent:<id>`; the worker acquires role
`queue-worker`, scope `worker:capture-recovery` — different key, so the dead
row sits there permanently and every later acquisition by the same uid fails,
immediately (no expiry is consulted), which the stand observed on the very
first post-kill worker run. Every role that goes through this registry —
capture, queue-worker, compile, doctor's repair, nightly, weekly — is behind
the same UNIQUE row.

**D3 — the queue's ownership projection is insert-only.** A worker killed
after `queue.queue_owner` entered (claim:before onward) leaves its row in
`queue_ownership` (`actor_id` PRIMARY KEY, `UNIQUE(canonical_role,
canonical_scope)`, `memory_queue.py` DDL). `_insert_queue_projection`
(`memory_queue.py:10511`) is a bare INSERT with no expiry consult and no
reclaim; only the exact owner deletes its row. After the registry-level
reclaim succeeds (same role+scope, no fence held — the claim-stage kills),
the next worker still fails with `UNIQUE constraint failed:
queue_ownership.canonical_role, queue_ownership.canonical_scope`, and there
is no code path that ever removes the dead row.

Severity ordering: D2 is the widest (one killed hook process silences the
whole operational plane for the uid, with only `capture_failures` counters
and `capture skipped` on stderr as signal); D1 and D3 are capture-worker
outages. In every case the crashed capture's evidence is retained — intent
files and rows block `run/` deletion by contract — so a later fix can still
land the content; nothing automatic will.

## What the property does and does not claim

- **Proven:** across 108 killed trials plus 2 clean sentinels, no injected
  process death produced content-gone-with-no-trace. Everything that did not
  land was either already durable in the vault, durably visible as an intent
  or task, or durably named as a failure. The daily block never appeared
  twice (no duplication was observed — though the wedges also prevent most
  retries that would exercise the replay-idempotence path).
- **Kill ≠ power loss.** SIGKILL tests process death at exact code
  boundaries: page cache intact, kernel keeps every completed write. It says
  nothing about fsync ordering, torn pages, or disk-cache lies under power
  failure — the failure classes ALICE and CrashMonkey exist for and that
  SQLite's own crash-testing simulates at the VFS layer. No power-loss claim
  is made here.
- **The clock advance is a simulation of waiting**, applied only to
  timestamps the product compares against wall time, and it was validated
  against real time: the D1 wedge reproduces identically after a real 125 s
  wait with no database edits.
- **One capture per vault, one writer at a time.** The stand does not measure
  concurrent producers/workers racing over one vault, and it does not measure
  the fate of *subsequent* sessions in a wedged vault beyond the named-trace
  observation.
- **The classifier is `fake`** (canned `FLUSH_MINOR`), so provider failures,
  timeouts and malformed model output are out of scope — the queue's
  deferred-work path for absent providers is not what was measured.
- **Producer entry** is `publish_capture_intent_from_payload` (the durable
  fallback publication); the hook-side delegate steps (`_tag_session_end`,
  the detached spawn) were not part of the kill surface. The detached spawn
  was not needed: the worker child is the stand's own, deterministic.
- The kill wrappers target the live classes the adopted runtime actually
  uses (`_QueueV3CandidateReader`, not the legacy `MemoryQueue`) — a test
  pins every stage name to a resolvable, callable product attribute so a
  rename fails loudly instead of arming nothing.

## Method context (current practice, checked 2026-08-28)

Kill-at-boundary testing of acknowledged writes is the standard cheap tier of
durability evidence, distinct from power-fault injection: Lucene's durability
stand drew the same line between `kill -9` and power loss; CrashMonkey/ACE
and ALICE generate crash states below the process (block IO / syscall
reordering) precisely because process death alone cannot produce torn or
reordered persistence; SQLite documents its atomic-commit assumptions and
tests them with a crash-simulating VFS. This stand deliberately occupies the
process-death tier and names the boundary.

Sources:
- [SQLite: Atomic Commit In SQLite](https://www.sqlite.org/atomiccommit.html)
- [SQLite: How To Corrupt An SQLite Database File](https://www.sqlite.org/howtocorrupt.html)
- [McCandless: Testing Lucene's index durability after crash or power loss](https://blog.mikemccandless.com/2014/04/testing-lucenes-index-durability-after.html)
- [CrashMonkey: A Framework to Systematically Test File-System Crash Consistency (HotStorage'17)](https://www.cs.utexas.edu/~vijay/papers/hotstorage17-crashmonkey.pdf)
- [CrashMonkey and ACE: Systematically Testing File-System Crash Consistency (ACM ToS)](https://dl.acm.org/doi/10.1145/3320275)

## Evidence

- Full run: `uv run python benchmark/run_durability.py --trials 108 --seed 0`
  (aggregate JSON frozen at `benchmark/durability-baseline-2026-08-28.json`).
- Deterministic repros of D1/D2 and of the recovery path:
  `tests/test_durability_stand.py` (8 tests; the wedge tests assert the exact
  named reasons).
- Real-time D1 proof: this note, section D1 (kill, 125 s wait, no edits).
