# What a reclaimer owes a dead owner: reclaim under process death

Date: 2026-08-28. Task: close `NEW-113`, `NEW-114`, `NEW-115`
(`docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`), found and reproduced by the
MEM-18 durability stand (`docs/research/2026-08-28-zero-silent-loss-stand.md`).
Status of this note: current-practice research for a change to the canonical
fenced ownership registry.

One-sentence summary: every lease system that survives a crashed holder pairs
*expiry* with a reclaim that removes **everything the dead holder registered**,
not only its lock row — this vault's registry removes only the row, so three
dependent projections (a foreign-keyed fence, a project lease, a writer gate)
and one cross-database projection (the queue's) outlive the owner they describe
and wedge the next process forever.

## The question this note has to answer

The existing contract is already written down and already correct: a row whose
lease has expired **and** whose process is provably dead is reclaimable; a live
owner, or one whose liveness cannot be established, is refused by name
(`scripts/operational_ownership.py::_expired_owner_is_dead`, and the
2026-08-26 coordinator-liveness decision that established "doubt refuses
closed" for the V3 migration — `knowledge/log.md`, 2026-08-26).

What is missing is not the proof. It is the *extent* of the reclaim. Three
questions:

1. When a lock row is reclaimed, what happens to state that only exists
   because that owner existed?
2. Is it safe to delete that state on behalf of a dead owner — could the owner
   come back?
3. What should the collision case do when the identity key of a dead holder
   blocks an unrelated acquisition?

## 1. Dependent state dies with its owner — the standard answer

**ZooKeeper** answers question 1 by making it impossible to ask. An ephemeral
znode "exists as long as the session that created the znode is active. When the
session ends the znode is deleted", and — precisely because that deletion has to
be total — "ephemeral znodes are not allowed to have children"
([ZooKeeper Programmer's Guide][zk]). A dependent that could outlive its parent
is not a corner case there; it is a schema the API refuses to let you build.

**Chubby** answers it by recording the dependents next to the owner so the
reclaimer can find them: "The database records each session, held lock, and
ephemeral file", and that persistent state is what lets a new master rebuild
lock state after a failure ([Burrows, OSDI'06][chubby]). Sessions expire on a
lease; the locks and ephemeral files held under an expired session go with it.

**Kubernetes** leases carry `leaseDurationSeconds`, `renewTime` and
`holderIdentity`: a leader that dies cannot release, so a challenger waits for
the duration to lapse past `renewTime` and takes over, incrementing
`leaseTransitions` ([Kubernetes: Leases][k8s]). The takeover is a write to the
one Lease object — Kubernetes has no dependent rows to clean because the lease
*is* the whole state.

This vault sits in the middle: like Chubby it records the dependents durably
(`intent_fences`, `project_leases`, `writer_owners`, and the queue's
`queue_ownership`), but unlike Chubby its reclaimer never reads them. So the
right shape is the one Chubby and ZooKeeper share — **the reclaim of an owner
is the reclaim of everything registered under it** — and the vault's own code
already assumes exactly that. `markdown_transaction.py::_leave_canonical_gate`
carries the comment: "What proves the loss is the projection row: reclaiming it
deletes this owner's row and bumps the fence, so a delete that removes our row
means nobody else ever took the gate." The reclaim that comment describes does
not exist yet.

### Why not `ON DELETE CASCADE`

SQLite would do this in the schema. With the default `NO ACTION` a parent
delete with live children is simply rejected — the documentation shows exactly
the error the stand observed, `foreign key constraint failed` — while
`ON DELETE CASCADE` "propagates the delete or update operation on the parent
key to each dependent child key" ([SQLite Foreign Key Support][sqlite-fk]).
Enforcement is off by default and must be enabled per connection; this runtime
enables it (`reliable_memory.py`, `PRAGMA foreign_keys=ON`, then asserts the
pragma took).

`CASCADE` is nevertheless the wrong instrument here, for two measured reasons:

- **The schema is frozen by the adoption record.** Offline V3 adoption writes
  `coordinator_schema_sha256` (`installed_memory_repair.py:496`, from
  `markdown_transaction.COORDINATOR_V3_SCHEMA_SHA256`) into the immutable,
  create-only adoption record, and every later validation re-checks the shape.
  Changing a `CREATE TABLE` changes that digest, so a DDL fix would refuse
  service on every already-adopted vault — including this one.
- **A cascade is silent and unconditional.** It would delete dependents on
  *any* parent delete, including the ordinary release path, and it would do so
  without the proof. The whole point of this change is that a deletion happens
  only under "expired **and** provably dead". An explicit delete keeps the
  proof and the act in the same place, where a reader can see them together.

So: explicit deletion, in the same transaction, immediately before the parent
delete, executed only on the reclaim path.

## 2. Is deleting a dead owner's dependents safe? The fencing argument

Question 2 is the one Kleppmann's 2016 argument settles. Expiry alone never
proves a holder is gone — a paused process can wake up after its lease lapsed
and still believe it holds the lock — so the resource must reject the stale
holder by token: the client gets a monotonically increasing fencing token on
acquisition and the resource "remembers that it has already processed a write
with a higher token number, and so it rejects the request with token 33"
([Kleppmann, *How to do distributed locking*][kleppmann]).

This registry already has that machinery and it is what makes the reclaim safe:

- `maintenance_owner_epochs` hands out a strictly increasing `last_epoch` per
  `(role, scope)`; `intent_fence_epochs` does the same per intent.
- Every projection row carries the canonical fencing tuple
  `(role, scope, actor_id, owner_token, fencing_epoch)`, and every check —
  `require`, `heartbeat`, `_release_in_transaction`, `_delete_writer_projection`,
  `_require_live_intent_fence` — matches on that whole tuple.

So a resurrected holder cannot be harmed by the deletion in the way a naive
lock could: its next call matches on its own token and epoch, finds nothing,
and fails closed with `owner_fence_lost` / `intent_fence_lost` — a named
refusal, not a silent overwrite. That is stronger than what expiry alone would
buy, and it is exactly Kleppmann's condition.

This vault also has a proof Kleppmann's setting does not: the OS. The registry
does not reclaim on expiry alone; it additionally requires
`process_identity_state` to return `dead` — a pid whose start identity no
longer matches (Linux `boot_id` + start ticks, Windows creation FILETIME,
Darwin `proc_pidinfo` start time). A pid that has been recycled reads as
`dead` because the start identity differs; an inaccessible pid reads as
`unknown` and refuses. Expiry narrows, the OS decides, doubt refuses.

## 3. The collision case: one identity key, many roles

`maintenance_owners.actor_id` is `UNIQUE`, and on POSIX the actor identity is
`posix-uid:<uid>` — one row per user for the whole machine. The reclaim path
only ever examines the same `(role, scope)`, so an acquisition under a
different role never looks at the dead row: its `INSERT` trips
`UNIQUE constraint failed: maintenance_owners.actor_id` and surfaces as
`owner_identity_conflict`, with no expiry consulted at all. One killed capture
hook therefore silences capture, queue-worker, compile, doctor, nightly and
weekly for that uid.

Practice is unambiguous that a uniqueness key which represents *a holder* must
be consulted with the same liveness rule as the lock itself. Kubernetes'
`holderIdentity` is not a second, exempt gate: a challenger reads the whole
Lease — holder, duration, `renewTime` — and takes it over as one decision
([Kubernetes: Leases][k8s]). Chubby likewise expires the *session*, and every
lock and ephemeral file the session holds goes with it, regardless of what they
name ([Burrows][chubby]).

The correct fix is therefore not to relax the constraint but to widen the
lookup: before inserting, consult **any** row holding this actor identity, and
apply the identical proof. Dead ⇒ reclaim it (with its dependents). Live ⇒
`owner_identity_conflict`, by name, as today. Unknown ⇒ `owner_liveness_unknown`,
by name, refusing closed.

## 4. The cross-database projections

The queue's `queue_ownership` lives in the queue database, not the coordinator,
so no coordinator-side reclaim can ever reach it, and `_insert_queue_projection`
is a bare `INSERT` — no expiry consult, no reclaim, deleted only by its exact
owner. It carries `expires_at`, `process_id` and `process_start_identity`: the
same three columns the coordinator proof reads. It therefore gets the same
proof, applied by the same code, rather than a second, similar-looking rule —
the lesson this vault already wrote down on 2026-08-24 about definitions that
exist in more than one place (`scripts/page_status.py`, "one definition, named
by what it excludes").

`task_fences` is the same story one step further in, and the stand only made it
visible once the first three were fixed: it is the queue-database twin of
`intent_fences`, carrying the identical canonical tuple, and
`_require_fenceable_task` refuses on bare row existence exactly as
`acquire_intent_fence` did. Measured: with the three registered defects fixed
and this one left, the `record-write:before` kill point recovered from
`IntegrityError: FOREIGN KEY constraint failed` to a permanent
`QueueOperationError: task_fenced` — a wedge of the same class, one layer down.
It is reclaimed with its owner's projection, in the same transaction and under
the same proof, which is what makes that kill point land instead of stalling.
The remaining hole is named in "What this note does not settle".

## How this fails closed

- Nothing is deleted without **both** halves of the existing proof: the lease
  expired against the registry's own clock, and the OS says the process is
  gone. `unknown` raises `owner_liveness_unknown` and deletes nothing.
- Marker-bearing roles (`compile`, `nightly`, `weekly`) still have their marker
  file re-validated against the dead row before anything is removed
  (`_lease_marker`), so a tampered or missing marker refuses with
  `marker_identity_invalid` instead of reclaiming.
- The deletion is scoped by the full fencing tuple, so it can only remove rows
  that name *this* dead owner. A projection belonging to a live owner does not
  match and is not touched.
- `capture_binding_projections` rows are deleted only when the fence row they
  were projected under is itself being deleted — the same pairing
  `release_intent_fence` performs on the normal path — because the coordinator's
  own shape invariant (`markdown_transaction.py`, `capture_projection_violations`)
  requires every binding projection to have a matching fence. Leaving one would
  trade a wedge for a corrupt-shape verdict.
- No blind delete and no swallowed error: every branch either has the proof or
  raises a named refusal.

## What this note does not settle

- **The runtime-deletion-check role stays conservative.** Acquiring
  `runtime-deletion-check` still requires literal quiescence and refuses with
  `runtime_deletion_check_requires_quiescence` when *any* other row exists,
  including a dead one. That refusal blocks a `run/` deletion permit, which is
  the safe direction, and the deletion contract is written in terms of owners
  present, not owners alive. Left as it is, deliberately.
- **A killed marker role still wedges on its marker file, not on the registry.**
  `acquire_compile_owner` publishes `run/compile.pid` with `O_CREAT|O_EXCL`
  *before* it acquires, so a killed compile leaves the file and the next
  compile fails with `FileExistsError` before any reclaim can run. That is a
  fourth defect of the same family, outside `NEW-113/114/115`, and it is not
  fixed here: removing another process's marker file is a non-transactional act
  that needs its own decision about who is allowed to do it.
- **A task fence whose owner projection is already gone stays.** The queue's
  `task_fences` rows are reclaimed with the `queue_ownership` row that names
  them. A process killed in the narrow window after `_remove_queue_projection`
  but before its fence was released would leave a fence nothing reclaims. That
  ordering does not occur on the normal path — fences are released inside the
  owner body, before the projection is removed — and the stand never produced
  it, so it is named here rather than fixed by guessing.
- **Kill is not power loss.** Everything here is measured at process-death
  granularity, the boundary the MEM-18 stand names; nothing in this note claims
  anything about fsync ordering or torn pages.

## Sources

- [Apache ZooKeeper Programmer's Guide — ephemeral nodes][zk]
- [Burrows, *The Chubby lock service for loosely-coupled distributed systems*, OSDI 2006][chubby]
- [Kubernetes documentation — Leases][k8s]
- [Kleppmann, *How to do distributed locking* (2016)][kleppmann]
- [SQLite — Foreign Key Support][sqlite-fk]

[zk]: https://zookeeper.apache.org/doc/current/zookeeperProgrammers.html
[chubby]: https://research.google.com/archive/chubby-osdi06.pdf
[k8s]: https://www.kubernetes.io/docs/concepts/architecture/leases/
[kleppmann]: https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html
[sqlite-fk]: https://www.sqlite.org/foreignkeys.html
