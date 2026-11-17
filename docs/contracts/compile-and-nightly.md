# Compile and nightly contracts

This part of the structure reference moved here from `docs/STRUCTURE.md` so every module
packet does not carry it. It is as binding as that file.

## Approved compile, queue, health and nightly refactoring

Approved 2026-10-17; implementation and installed qualification are pending.
The decision is `knowledge/notes/compile-queue-health-nightly-refactoring-decision-20261017.md`;
the design and research are in
`docs/research/2026-10-17-compile-queue-health-nightly-refactoring.md`.

Compile planning preserves complete verified source units and required context.
An independently refused unit does not prevent other units from completing;
the day remains incomplete until every required disposition is confirmed.
Provider capacity and a preferred working batch size are distinct. Unknown
provider capacity is never represented as verified. Existing receipts, source
hashes, transactional publication and at-least-once queue delivery remain.

The approved health interface is `doctor --scope full|operational`, defaulting
to `full`. Automatic session context and MCP explicitly request `operational`.
Reports state their scope and coverage; operational health neither certifies
historical integrity nor grants runtime deletion eligibility. Complete SQLite,
foreign-key, semantic, artifact and source checks remain in the full scope.
The existing `logs/doctor-report.json` continues to hold the full nightly
report; an operational observation does not overwrite it. Consumers and the
report schema change together.

Nightly maintenance remains one dependency-ordered pass. Its overall result
distinguishes completion, deferral and failure, and refers to the actual
compile work rather than inferring success from unrelated global timestamps.
No database, daemon, dependency or runtime root is added. A queue schema/index
migration, if needed, requires its own approved migration design.

### Approved durable compile progress (2026-10-19; candidate not installed)

The owner approved one append-only `compile_work_events` table in the existing
queue database, with an explicit offline schema migration. The current stage is
the latest event for a work identity; there is no second current-state table.
Intermediate artifacts use the existing `run/queue-results/` with explicit event
references included in retention, backup, doctor and deletion guards. Existing
queue ownership and payload verification fence transitions; terminal queue results
are not reused as intermediate checkpoints. Markdown transaction receipts remain
the authority for published work. Index refresh has a separate outcome.

The candidate connects CLI, MCP, queue and nightly to the same transition controller.
The candidate worker supports an explicit `LeaseProcessor`: after claim, it binds
the canonical queue handle, lease and caller deadline into the existing spawned
processor. Tokens stay outside user payloads. The parent keeps heartbeat and child
cleanup ownership; queue operations recheck the lease and payload in SQLite.
Ordinary processors retain their compatibility task dictionary. Ordinary CLI and
MCP compilation enqueue leased work; nightly invokes the same CLI. Dry runs do
not enqueue work.
A stored
model answer survives process restart. An uncertain dispatched request whose
answer cannot be recovered blocks automatic repeat expense until explicitly
authorized; a restart or provider fallback alone is not authorization. Current
provider adapters do not offer verified recovery of that response. The isolated
controller has an internal explicit-repeat transition bound to the exact dispatch,
actor and reason. It first recovers an available answer, preserves history, and
authorizes only the same request. The candidate's explicit operator entrypoint is
`memory_queue.py compile-recover <task-id> --work-id <work-id>
--dispatch-digest <digest>`. Without additional flags it recovers a saved answer
or leaves the unknown outcome blocked. `--accept-unknown-cost --reason <reason>`
permits one repeat of that exact request when no answer can be recovered.
Ordinary unblock/redrive never grants that authorization.

The controller's `resolve_model_call` distinguishes a recovered response, retained
repeat consent and an unresolved outcome. It verifies the original dispatch and
semantic checkpoint, checks current task ownership and reuses the existing answer
recovery path. An existing repeat decision remains the original immutable event
when a redrive changes delivery ownership. The controller operation neither invokes
a provider nor settles the queue task. The CLI surrounds it with canonical
queue-operator ownership, owner/task heartbeats and fenced queue settlement.
A recovered response is not proof of a valid compile plan or completed work.

The candidate queue CLI exposes `memory_queue.py compile-progress <task-id>`.
It reads the task's verified redrive lineage and latest checkpoints in one read
transaction, returning work IDs, event digests and recorded model-call metadata.
It does not print source text, model answers or arbitrary journal details, and
does not unblock work or authorize a repeat. `answer_artifacts_checked: false`
and `history_audited: false` distinguish this view from answer recovery and a full
history audit. In particular, recorded dispatch without recorded answer does not
prove that no orphan answer exists. Corrupt lineage/checkpoints produce a failure,
not a partial success. The command requires the adopted compile journal.
`recovery_dispatch` identifies the original request even after its resolution was
recorded; its digest is the one the recovery command accepts.

Recovery can acquire a blocked unknown call or an unresolved ready delivery
(including an explicit redrive). It retains `provider_outcome_unknown` in
`blocked_capability` while leased: the original cause remains until the operator
handoff completes. The dispatch-record transaction refuses a new model call under
such a lease. Expiry restores the block and returns the administrative claim's
attempt increment, while retaining a `lease_expired` history entry. Ordinary
execution-lease expiry retains its existing attempt policy.
Successful recovery/consent records a successful administrative attempt and makes
the task ready; an unresolved outcome records a blocked attempt and leaves it
blocked. Both return only the administrative increment, preserve previous
execution/unknown-cost history and release the task lease in one transaction.
Cancelled/dead work is not silently reopened: the existing explicit redrive
operation handles dead tasks. Repeating the command after release returns
`not_blocked` with observed state and filtered work metadata, without a new consent
record or execution. Already-recorded consent after a crash is reused without
requesting it again. Canonical takeover still requires an expired, proven-dead
owner; live or uncertain owners are not bypassed. Installation and real full-cycle
qualification remain unfinished.

No new database, daemon, runtime root, MCP tool or package dependency is authorized.
Migration must reject old clients, preserve retained evidence, and run only under
the existing maintenance fence after qualification on an isolated copy. The
decision is `knowledge/notes/compile-durable-progress-decision-20261019.md`.
Approval is not implementation or installed qualification. All nine development
laws, complete-cycle validation and removal of replaced code remain required.
The isolated implementation now shares journal/artifact auditing across full
doctor, backup certification and runtime deletion guards. Ordinary queue cleanup
preserves files named by any retained compile event and reports retained tasks
without blocking deletion of unrelated finished work. Reference checks and unlink
share the queue write transaction with checkpoint artifact validation. This is
reference safety, not completed event retirement or orphan collection. The isolated
`compile_controller.py` additionally journals a model-call reservation before
external dispatch, recovers validated answer artifacts and their usage, and blocks
unknown outcomes through the existing queue. The controller reuses semantic
validation, journals correction progress and checks publication receipts, with
retained work/source/dependency identity. Migration deployment qualification,
complete artifact retirement and useful full-cycle evidence remain pending; see
`docs/research/2026-10-19-durable-compile-progress.md` for verification scope.
The candidate-only schema step atomically creates the journal and stamps queue
user_version4, refusing the active queue and inode aliases. The development branch
also has an explicit offline upgrade API with retained backup-image validation,
maintenance fencing and recoverable admission publication. It requires confirmation
that all agent processes are stopped; a live/unknown registered owner still refuses
the operation. This is not an online migration of existing SQL connections. The
installed vault remains version3; deployment and full compiler qualification are
pending. Compatible readers are implemented in the isolated development branch.

The approved migration's admission transition uses a version-2 envelope at the
existing `run/reliability-v3-adopted.json` path. It retains the complete original
version-1 record and its digest, the new queue file identity, the compile schema
digest, and the migration operation/backup-manifest digests. Original migration
records, tombstones and coordinator contracts remain verifiable. Version-1
admission continues to require queue version 3; the version-2 envelope requires
queue version 4 with all journal protections. No automatic migration is added to
client admission. Publication/recovery and installation still require qualification.

The offline upgrade uses the existing adoption envelope as the durable intent:
after a coherent backup, it publishes the version-4 requirement before applying
the transactional schema change to the existing queue inode. The mismatched
intent/version refuses normal admission until explicit recovery finishes. This
avoids renaming an operational SQLite database or adding a second activation
pointer. Recovery must verify the supplied external backup image against the
envelope digest and original adoption evidence; it never restores an old database
over new queue work. The backup path is an explicit operation argument, not a new
runtime location or an automatically deleted staging context.
After process death, recovery waits for the existing lease expiry and verified
process death; it does not force an owner away. Before intent publication, a failed
backup is kept and a retry requires a fresh empty image directory. The retained
image is owner-protected plaintext staging, not a claim of completed encrypted
Restic recovery. Encrypted recovery/installation qualification remains required
before deployment. No automatic migration is wired into startup or nightly.

The existing repair CLI exposes the explicit operation:
`repair_installed_memory.py --apply --upgrade-compile-progress --backup-image <absolute-directory> --confirm-all-agents-stopped`.
It is mutually exclusive with initial ownership adoption. The image must be an
external empty directory for a new upgrade, or the exact retained image for
recovery. Confirmation never overrides a live or unknown registered owner.
Success verifies the upgraded admission; it does not certify encrypted recovery
or authorize preimage deletion. The shell/PowerShell installers invoke the same
operation with `--compile-backup-image <absolute-directory>` /
`-CompileBackupImage <absolute-directory>` plus their explicit stopped-agent
confirmation. Validated inspection exposes `details.compile_progress` as
`upgrade-required` or `upgraded`; invalid/incomplete admission exposes neither.
An upgraded installation needs no image. New fresh adoption creates the compile
journal on the empty unpublished queue candidate before activation. Its version-3
adoption record pins queue schema 4 and coordinator schema 3, without inventing a
previous installation or backup. It uses the same database, migration-record and
tombstone paths. Old plans resume their recorded schemas; existing version-1
adoptions still require the explicit backed-up offline migration, and version-2
upgrade envelopes retain their prior-record and backup proof. Normal admission
never upgrades an active database. The same initial-adoption API serves both
installers, whose validated ready branch needs no backup flags. An existing
installation requiring migration without explicit inputs reports a warning.
No image directory is chosen or deleted automatically. An isolated migrated fixture has passed backup and
restore using real Restic 0.19.1, including exact Markdown and queued payload
checks; the installed private vault has not passed this deployment qualification.
