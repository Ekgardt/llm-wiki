# Durable compile contract

The storage, streaming, batch, repair and publication rules of the durable compile path, moved word for word from `docs/STRUCTURE.md` on 2026-11-09 so that every other module packet no longer has to carry them.

The uninstalled compile-progress branch also connects its journal to the shared
post-DLP, post-preparation dispatch boundary. Candidate results retain safe output,
failure classification and usage. Indexed historical event reads let `CompileReplay`
feed saved answers back through the existing `resolve_compile_plan` validators;
unknown transport outcomes remain blocked. The candidate semantic caller binds and
retains work/source/dependency identity and supplies this runner through the
common leased entrypoints. This integration does not activate queue schema 4 or replace
the installed controller; deployment and complete useful-cycle qualification remain
pending. See `docs/research/2026-10-19-durable-compile-progress.md`.

The uninstalled durable compile controller also stores lossless input snapshots in
one existing queue-result artifact: content-addressed bytes inside the envelope,
original partition/provenance metadata, prompt projections and target context.
Its checkpoint binds the canonical manifest of record digests. It adds no recursively
referenced artifact tree. The streaming format replaces the uninstalled JSON/base64
codec; source capture and batch planning still require incremental consumption
before routing installed compile entrypoints through this mechanism.

Owner-approved target (2026-10-19): large compile artifacts use streaming local
file I/O under existing `run/queue-results/`, with immutable references in the
existing journal. Persist and verify bytes before publishing their reference;
health, recovery, backup and retention must follow the same identity checks.
The compiler must also consume snapshots incrementally before this is considered
complete. A streaming file verifier alone is not that completion. No new runtime
root, service or cloud dependency is required. This supersedes raising the inherited
small-result ceiling as the solution for large snapshots; ordinary small queue
results retain their existing compatibility contract during uninstalled development.

Streaming snapshot implementation target within the approved artifact contract:
one uncompressed ZIP64-capable queue-result file, containing digest-named raw
byte members and individually addressable record metadata. No archive member is
extracted into the filesystem. The queue still retains one immutable artifact;
there are no recursive external blob references. A context-managed reader exposes
record metadata and selected records, allowing the batch planner to avoid loading
unselected source bodies. ZIP directory metadata is held in memory; source bodies
are not loaded by opening the container. Python's standard library supplies the
container format; there is no new package, runtime path or operational database.

The uninstalled batch controller admits a verified preflight entry into the same
compile journal before reading mutable context. Its stable work key survives
redrive; exact retained parent/entry bytes and dependencies are checked. A byte
offset addresses the entry without decoding the whole plan again; the existing
file boundary still verifies its complete digest. Pending admission alone grants
no model call or publication. Its saved context binding is described below.

Batch and repair roots also bind `compile-source-scope/v1`: logical paths, byte
spans and source hashes derived from the verified snapshot. Before recording a
model dispatch, the existing queue write transaction checks other work for
overlapping source spans and unresolved calls. The check and reservation share
the same lease/payload fence and `BEGIN IMMEDIATE`; there is no separate lock,
table, index or database. Repartitioning or appending a day cannot bypass an
overlapping span. Rewriting that span does not establish independence; disjoint
spans and different paths may continue. Historical work without scope cannot
prove independence from an uncertain call.

The guard verifies journal rows, follows the tail to the latest model call and
uses the existing answer decoder to validate retained bytes against their
dispatch. A file hash or a `published` status alone cannot clear uncertainty.
An orphan answer must first be recovered by its owning work. Explicit repeat
consent does not release another work before a durable answer exists. The
semantic controller blocks the competing task through the existing unknown
outcome delivery path and may continue independent roots. This protects the
candidate queue only; it does not recover unjournaled legacy calls or provide
semantic deduplication of copied sources. The metadata scan is linear in retained
work count; archive/retention and full-cycle cost qualification remain required.

The verified-lease boundary also compares the caller's kind, handler version and
input hash with the stored task, then hashes the caller's canonical payload.
The payload is not redacted again during this comparison: redaction could conceal
a changed value. This applies to heartbeat, protected compile transitions, result
publication and settlement. A mismatched caller is refused without changing a
valid task; stored payload corruption still follows the existing demotion path.

Candidate compile tasks can carry one `compile_request` with schema
`compile-request/v1`, a canonical vault-relative daily `file` or null,
`closed_days_only` and `trigger`. Leased task fields and types are checked before
the child compile lock or provider discovery; foreground encoding is checked
before queue admission. Explicit files retain precedence over closed-day
filtering. The controller binds these options in `leased-compile-request/v2`
inside its existing snapshot admission. Recovery reads saved sources even if the
live daily file has disappeared. New task encoding resolves the CLI path once;
restoring that intent does not resolve or inspect the live file again.

Historical tasks without `compile_request` keep their original all-pending/auto
meaning and retained v1 request identity. This reader compatibility is needed
until retained historical work is resolved; it does not permit new frontends to
omit selection intent. The task encoder excludes ephemeral lock ownership and
refuses dry-run requests. In the uninstalled candidate, CLI, MCP and the nightly's
CLI invocation route mutating compiles through the shared supervised executor.
Dry-run remains a nonpublishing preview. The foreground requires the adopted
compile-journal schema; it does not fall back to legacy mutation on an older queue.
Preview does not repair the source-state mirror, retire source failures, or
record its refusals and errors as failures of real compilation. Failed previews
still report failure and leave the last real completion outcome unchanged.
This does not make legacy preview model calls durable: it still uses the existing
plan cache, pending a separately agreed preview intent in the common controller.

The adopted queue and its existing supervised worker accept an optional exact
task ID. Selection keeps the same transaction, eligibility, payload checks and
lease acquisition; an unavailable target never falls through to unrelated work.
The worker retains parent heartbeats, child supervision, result adoption and
whole-work settlement. Ordinary workers keep their existing ordering. This
internal capability is qualified with a spawned compile child. The leased
entry accepts ephemeral compile-lock authority through its processor argument,
using the existing spawn-token check. This token is never retained in the task
payload or source-selection identity; a borrowing child does not release its
parent's lock.

The candidate worker propagates the caller's optional cancellation callback
through its existing parent supervisor. It checks before claim and spawn, during
busy claim retries, while awaiting a child answer and while joining the child.
The callback stays in the parent; lease-loss interruption remains independent.
A canceled running task is blocked with `caller_cancelled`, so automatic workers
do not restart it. Explicit unblock does not clear a retained unknown model call:
the compile controller still requires resolution or repeat consent. Completed
answers already in hand retain normal validation and cleanup. Custom injected
synchronous runners retain responsibility for their own interruption.

Foreground request admission selects the oldest unfinished leaf with identical
compile-handler/payload identity, or creates a task when no such leaf exists.
It claims that task in the same SQLite transaction through the ordinary claim
implementation: no other worker can steal a newly admitted foreground task
between enqueue and claim. Blocked, dead or leased tasks do not become new work
and do not have attempts reset. Explicit redrive selects the replacement leaf;
succeeded/canceled leaves allow a new request. Payload/digest mismatch refuses
reuse. A resumed capture retains its original sources; subsequent source edits
remain separate pending work. There is no permanent selection-only dedupe key.

The parent projects a queue-worker owner in the existing canonical registry,
renews that owner and its queue projection, and passes verified owner authority
to the child for publication. Supplied parent ownership is preserved. The shared
worker supervises both ordinary claims and atomic foreground claims, including
retained-result adoption and whole-work settlement. Foreground success requires
the task's succeeded state; other retained states report the task ID and reason.
Worker results travel over the existing dedicated pipe; the child redirects its
stdout descriptor to stderr before entering the handler, preserving MCP stdout
for protocol messages. Parent completion diagnostics also use stderr.
Installation, full dependency/limit qualification, semantic retention/backup and
the real medium-provider full cycle remain required before product completion.

The uninstalled refreshed-context format stores only complete changed context
records and explicit removed paths in another self-contained ZIP64 artifact.
It binds one complete source capture by manifest digest; it never follows delta
chains or external blob references. An open-reader overlay restores all context
collections, including unselected publication targets. The candidate batch controller now saves/replays this delta through the existing
queue artifact boundary. Archive binding identifies its original admission and
source snapshot, including when recovering an orphan before reading live context.
Both files must remain owned and retained by the same durable work; queue-level
semantic reference enforcement and retention/doctor/backup traversal are still
required before production routing. The candidate reader validates the complete
restored context while keeping both archive lifetimes open.

The candidate publication checkpoint saves accepted plan bytes, action key,
provider budget and original timestamp before Markdown writes. Its publication
action key binds the original model-request key plus the complete accepted plan;
`request_action_key` keeps the original cache identity distinct. The uninstalled
publication document/binding is v2; receipt schema remains v4. It binds the saved
context, selected batch and dependencies; recovery reuses the recorded/orphan file.
This is publication intent only. The candidate confirmation path verifies complete
source and companion receipts plus the committed Markdown transaction under the
existing writer gate, then records a batch's `published/completed` event. It checks
vault/state/coordinator identity and revalidates prior completions on retry; a lost
receipt is an error. No complete proof leaves intent pending. This does not finish
the whole queue task, and quarantine is not success for unreceipted sources.
The candidate execution helper reconciles receipts first, applies only the verified
saved document when incomplete, then reconciles again. It rechecks queue ownership,
dependencies and cancellation inside the existing apply admission checks. A recovered
completion skips apply; an apply return alone cannot establish source completion.
Production entrypoint routing and whole-task finalization remain unfinished.

The candidate prepared-batch scope now uses the saved context with the same
semantic selection/identity checks as live refresh. Joining selected daily sources
with archive context retains metadata positions, not all context bodies. The scope
owns archive lifetimes across preparation and consumer work. The candidate planned
batch executor now joins that scope to semantic answer replay, accepted-plan storage
and receipt-confirmed publication. An already accepted intent bypasses resolution
and the model cache. Repair-capacity and uncertain-call exceptions propagate to the
delivery owner, and a caller's existing CompileDelivery can span independent work.
The return confirms one batch only. Whole-task accounting, production entrypoints
and repair-child routing still need integration before install.

The candidate planned executor now saves capacity-driven repair decisions before
propagating the failure. The `review_ready/waiting` event binds the parent context,
batch, dependencies and original correction feedback. Its immutable artifact holds
the deterministic partition records; the reader reconstructs them using the existing
partitioner and checks exact source coverage, smaller complete child work and retained
publication context. Child identities bind that recorded decision and their full
record, including feedback. An uncertain dispatch cannot authorize a partition; a
waiting repair cannot be used as an ordinary model-call checkpoint. Recovery reads
the retained failure without resolving the parent again. No partition remains an
explicit unresolved refusal. Child admission now verifies the exact retained
decision, full child record/feedback and inherited publication context. Its initial
event binds the repair decision; its ready checkpoint refers to the same immutable
parent context artifact. It creates no queue task or copied context archive.
The candidate child executor passes retained feedback into the shared semantic
replay/publication path, so the existing correction cache guard remains effective.
The candidate composite confirmation walks nested repairs in post-order using an
explicit stack, rechecking all leaf source/companion receipts and transaction
authority under one canonical Markdown writer gate. Parent completion binds the
recorded repair and immediate child completion digests; it does not claim a shared
transaction. Missing child evidence produces no new completion; invalid evidence
after a recorded completion is an error. Confirmation never admits absent children
or calls a model. The candidate tree executor now walks saved partitions with an
explicit stack, admitting each child only when reached. It preserves the original
archive scope and correction feedback. Capacity failures create or reuse retained
partitions; indivisible failures leave completion absent. Unknown model outcomes
allow disjoint source siblings to progress inside the same delivery, but block
that delivery when it closes. They never authorize a repeat or parent completion.
Other errors propagate immediately. Final completion revalidates the whole tree;
the executor does not settle the queue task. The prepared-tree entry owns archive
lifetimes and can participate in an existing delivery.
The candidate whole-work executor now consumes every verified preflight entry
inside one delivery and asks for full-work proof afterwards. Refusals remain
durable in the preflight even when there is no notification callback; they prevent
completion. The proof walks all root trees under one canonical writer gate using
only existing saved child contexts. Missing work is not admitted. A compact
`work_completion` event binds the exact preflight, batch count and ordered hash of
all revalidated root completion records. A truly empty source manifest may
complete; an all-refused manifest cannot. Previous completion is revalidated, and
lost evidence is an error. This proof does not settle the queue task: the worker
parent still owns that transition after the entire delivery exits. Queue schema4
now requires the original lineage's whole-work attestation before writing final
result bytes, again when binding those bytes, and when acknowledging a compile
task. It validates the attestation's shape, source identity and exact retained
snapshot/preflight binding in the verified-lease transaction. This is validation
of a durable past completion, not another live receipt snapshot. A truthy processor
response without proof blocks with `compile_work_incomplete` and capability
`compile_completion`; malformed proof reports `compile_completion_corrupt`.
Other task kinds and the pre-adoption schema3 retain their result contracts.
The existing parent worker handles this refusal without counting success or
automatically retrying an invalid success response. Production dependency
preparation, entrypoint routing and semantic reference auditing remain unfinished.

The candidate initial admission captures an immutable archive before recording
either root0 or snapshot1, then appends both in one verified-lease transaction.
The durable queue task is the intent before capture. Its stored ZIP64 v2 manifest
binds original task lineage, payload, dependencies and request; its manifest
digest identifies the admitted snapshot. Source bytes are captured in one live
iteration, then retained bytes are reread for validation. A complete orphan is
validated and adopted before considering live source selection. This avoids a
root with a digest that changed live sources cannot reproduce after interruption.
Dependency changes during capture, an expired deadline, lost ownership or invalid
archive prevent admission. Compressed archives are refused before manifest reads.
Existing v1 archives remain readable. A previously admitted root lacking both its
snapshot and a matching complete orphan is reported, never silently rewritten.
Production routing must adopt this path before the independent root/capture
helpers can be removed.

Saved-snapshot and initial-orphan validation share a streaming body verifier.
For daily records with an original, validation also proves that the part's
content hash matches its declared original byte range. The original member is
read through a context-owned range view: a complete pass proves its whole hash
and records block hashes; subsequent reads verify blocks and retain only the
current I/O block. Consecutive records reuse that open original; a different
digest or scope exit closes it. Deadlines and closed-scope checks apply to cached
reads too. Rewinds use zero plus bounded reads so no forward ZIP seek performs a
large hidden read. This adds no persisted block index
or temporary file. The planner's restored original and live capture still use
whole bytes until the remaining native-frame and proof consumers migrate.
Each referenced byte blob is hashed through EOF using the existing I/O transfer
buffer; only verified digest identities are retained for that pass. Record
metadata must bind its content/original digests, satisfy the existing snapshot
constructor's required/accepted fields, and pass native-frame/entry conversion.
Validation does not construct full snapshot bodies. Expired deadlines refuse
before archive opening and are checked between body reads. Current shared JSON
metadata is reused; manifests and JSON metadata still load in memory. The outer
queue artifact digest, descriptor identity and lease checks remain required,
and later planning still has its own semantic source/evidence checks.

The candidate tree executor also distinguishes exhausted plan resolution
(`CompilePlanUnavailable`) from arbitrary runtime failures. It records that
source's failure through the existing diagnostics before continuing independent
siblings and roots. The failed source remains incomplete, preventing whole-work
proof and queue success. Re-evaluating saved calls does not dispatch them again.
A failure to write diagnostics propagates, as do unrelated runtime, storage,
ownership and dependency errors. Capacity failures keep the retained partition
path; unknown outcomes keep the delivery's explicit blocked state. This change
does not authorize a new model attempt or retire unresolved source evidence.

Run planning now has one compiler preparation path for eager packing and the
candidate streamed controller. It owns the resolved candidate order and selected
planning model, feeds the existing archive iterator, and supplies a digest-only
request identity covering descriptors, endpoints, resolution status, Codex
planning evidence and executable identity. Replay of that prepared plan does not
rediscover providers. This request binding complements the live dependency
witness; it does not establish complete environment closure. The shared
protected-call identity encoder converts native tuple evidence to JSON arrays
before canonical hashing, preserving configuration and executable identity
without relaxing the canonical serializer.

The candidate dependency factory composes product, installed-package, Python
runtime and configuration inventories into one re-inspected witness. Configuration
binds settings bytes/effective values, scoped model choice, authenticated DLP
policy, vault identity and the actual inherited provider environment. Only
digests escape that configuration reader. Changed observations before/after
inventory refuse admission. All inherited environment keys participate because
the existing CLI child receives them; even a benign environment change therefore
changes this witness. This does not lock external editors, exclude ABA races,
prove loaded-code/OS-library closure or replace update fencing. Measured candidate
capture/verification cost was 4.1s/2.82s on the current Python3.12 test environment;
full-cycle cost remains a qualification requirement.

The candidate queue CLI now uses the lease-aware processor. Schema4 compile
handler v1 enters `run_leased_compile` under the same compile lock, receipt scope
and call ceiling as the legacy entry. It prepares planning/dependencies, performs
fresh source/receipt maintenance only when initial capture is needed, admits or
recovers the saved snapshot, and executes the whole work. It returns success
only with completion proof; the parent worker retains heartbeat, child cleanup
and queue settlement. Unknown handler versions refuse before compilation.
Other task kinds and pre-adoption queues retain their handlers until migration.
The admission archive now carries `compile-capture-admission/v2`: the original
request and retained planning evidence accompany their digests. Recovery opens
that owned snapshot before remote discovery. Local configuration reconstructs
the candidate objects; saved Codex model/catalog/executable/configuration
observations restore the original plan. Endpoints are represented by digests;
raw configuration/environment content is absent. Current dependencies still
must match. A wholly failed model-resolution pass admits no immutable plan.
Older digest-only admissions remain readable evidence and cannot supply
execution planning that they did not retain.

The semantic replay cursor records a ready provider selection as
`compile-provider-probe/v1`, including preceding unavailable candidates, before
any model dispatch. It replays that same selection without reprobing. If every
candidate is unavailable, no selection or model dispatch is recorded, so a later
attempt may repeat the read-only probes. Interrupted observation publication may
also repeat probes; unknown model outcomes retain the separate no-redispatch
contract. Runtime transport safety checks, including local-only enforcement,
remain active; this does not promise recovery across changed dependencies.

This is an uninstalled worker integration. CLI/MCP/nightly CLI ingress is wired
in the candidate; per-batch diagnostic projection and full
dependency/retention/installation qualification remain unfinished.

Receipt discovery retains parsed bodies only for the current daily file, keyed
by its logical path and existing file identity. Switching sources releases that
view before reading the next source; returning to an earlier source rereads it.
Receipt metadata and prefix hashes remain separate. This removes cumulative
daily-body retention during maintenance without discarding any sources or
introducing a configurable corpus/cache limit. A single daily is still parsed
in memory, and this is not a constant-total-memory guarantee.

The archive's eager compatibility planning view keeps daily records materialized for existing partition
proofs, while context source/target/vault collections retain verified record indices
and logical paths. Selection filters metadata before reading bodies. Ranking stores
word statistics and indices; byte sizing stores numeric frame sizes. Required
citation association still scans every context page. The view is owned by the open
archive context and must not escape it. Source capture and daily-part planning now
have the streaming paths described below; installed entrypoint routing remains
unfinished. The eager view remains for existing replay/compatibility consumers
until they adopt the streamed controller and their recovery tests pass.

The capture iterator emits one source's records at a time into the same snapshot
container. The ordinary in-memory capture uses that iterator too and retains its
existing partition-proof cache. The streamed writer retains record metadata and
blob digests, not completed source bodies; source/target metadata is sorted before
publishing the manifest so logical identity matches the ordinary snapshot. It
returns the manifest digest only after successful archive closure. This format
writer is not itself a queue admission or publication operation: the durable queue
writer and complete-work binding must surround it before production routing.

Knowledge-note capture uses the shared `MAX_KNOWLEDGE_PAGE_BYTES` boundary
that also governs compiled after-images. It must not apply the smaller vault
metadata input bound to an otherwise supported note. Capture preserves the
whole note and its digest, including through retained archive validation and
reopening. This alignment does not establish a measured justification for all
remaining size limits; that review remains separate.

Streaming capture constructs daily part bodies on demand and does not populate
the eager caller's per-part proof registry. Receipt filtering retains the current
indivisible native/tool event and lookahead; completed companions remain when any
part of that event is pending. Existing list-returning consumers collect the same
iterator. Original daily bytes and entry/frame metadata still remain in memory:
this removes all-part buffering, not the remaining whole-daily read.

The archive range reader and evidence resolver share the existing low-level
`bounded_io.VerifiedByteRange`. Its verified size, digest, block hashes and
transfer geometry are frozen; only its read cursor changes. A resolver accepts
this exact type or the existing immutable bytes contract, never an arbitrary
object claiming a digest. It checks range ownership/deadline even when reusing
entry metadata. Full-source UTF-8 validation and physical newline indexing are
incremental; complete cited-block hashes use verified ranges without copying
the block. Each verified source lazily owns one immutable physical LF-position
index shared with the resolver. LF searches use binary search instead of
rescanning every source prefix per quoted row. Raw line indexing does not mark
UTF-8 valid: full strict decoding is required separately before evidence reuse.
The metadata scales with line count, and remains subject to source lifetime. The requested citation still materializes for the existing result
contract. Closed owners, changed reloaded blocks, bad UTF-8, and mismatched
references fail. Cached blocks remain the original immutable captured bytes.
Archive restoration now supplies these ranges to normal record access, required
citation checks, preflight and batch planning. Source receipt-prefix checks use
the same range hash proof. Live snapshot capture uses an owned file range,
but daily preselection, eager compatibility and native-frame payload retention
still have whole-body paths and are not claimed to be fully streaming.

Candidate transaction preparation also captures before-images as owned verified
ranges. POSIX opens remain relative to a held parent descriptor; Windows retains
its existing parent handle. Source hashes, append-prefix comparisons and compressed
image writes consume blocks within that ownership scope. Final content and parent
identity checks precede returning staged work, including absent-file checks for
creates. Parent-identity migration uses the same capture scope. Image formats and
undo contracts are unchanged. Image loading and digest checks share one owned
plain/XZ input stream; undo descriptor verification hashes without materializing
plaintext. Apply/undo still materialize plaintext for their bytes contract and
full publication DLP checks, but no longer retain the full encoded image as well.
This candidate change is not proof of live installation.

Fallback receipt lookup now opens a verified source scope and yields parts
incrementally. Digest lookup, saved-source matching and committed-failure
resolution must finish inside that scope; source closure validates the final
hash before returning a result. The selector retains no whole-day body or part
list between queries. Normal capture passes its existing part directly to the
selector. Fresh fallback queries intentionally verify the current source again.
Fresh verification costs more than the old whole-day cache; full-cycle cost
qualification remains required. Entry parsing uses the same grammar on verified
windows ending at LF or actual EOF. A line exceeding transfer geometry selects
the complete range parser, never rejection or truncation. Part content is read
once and the same bytes supply its digest, avoiding stream rewinds at block
boundaries. These changes reduce the measured scan from 0.385s to 0.053s, versus
0.018s for eager reading; this fixture is not installation qualification.

Live file ranges compute the original digest and per-block proofs in one pass.
Their descriptor uses the shared no-follow/regular-file admission checks and
unbuffered reads. Each use checks the descriptor/path identity and metadata.
On successful scope exit, a fresh full hash verifies the source again before
the capture can publish its manifest: rapid equal-size rewrites can leave
timestamps unchanged. Caller errors close the descriptor without publishing.
This preserves the existing cooperating-writer fence and does not provide
an OS filesystem snapshot against hostile concurrent writers. No new runtime
directory, package, or numeric cap is introduced. Existing daily/total-source
ceilings remain pending the law-9 audit.

The native journal lookup is a read-only mapping of body offsets to source
heads and physical spans. It retains the source reference and metadata, not
copies of every matched block. A requested value reads its complete block;
head-set discovery reads metadata only. Candidate discovery accepts verified
range sources and preserves the old LF-only, strict-path-UTF-8, non-overlapping
grammar. Mutable buffer inputs are copied once to preserve stable lookup
results. Native frame encoded/text values still remain in the frame tuples and
archive metadata; removing matched-block copies does not finish that migration.

Canonical daily entry identification uses byte ranges instead of copying entry
bodies. Heading identifiers need no body read; captured marker identifiers use
incremental UTF-8 decoding and block searches with the existing Python Unicode
line/whitespace semantics. Only the timestamp grammar prefix is retained after
unbounded leading or bullet whitespace. Byte spans and malformed-UTF-8 replacement
behavior remain unchanged. This does not remove the original daily buffer.
Boundary discovery itself uses length, byte searches and bounded slices, so it
also accepts a range source without a whole-file buffer. Delimiter line endings
remain EOF, LF or CRLF; Unicode content-line semantics do not widen delimiter
matching. Heading ID validation decodes through the complete ID incrementally,
including after a grammar rejection, preserving strict UTF-8 error priority.

Sequential daily reads restore original sources as verified ranges. The reader
holds a weak registry keyed by logical path and digest; live parts keep their
source alive across day/batch lookahead, and a released source closes its member
stream. The reader closes all remaining live ranges before its queue-owned
artifact context exits. Reads also check the ZIP owner, because an open ZIP
member can otherwise outlive it. Metadata reuse remains scoped to the current
original_entries/native_frames identities; part bodies are read and verified
without accumulating in the reader. Selected part content stays bytes even when
its digest equals the full original digest. Each record checks its declared
original identity, and part/receipt checks retain canonical range provenance.
No arbitrary object with a digest can supply this proof. Byte inputs retain
their computed exact-object proof; verified ranges reuse their frozen computed
identity and check their live scope. There is no corpus-wide body cache or
numeric eviction threshold. Canonical record serialization reads dataclass fields
without deep-copying range owners, and ZIP writes stream known-size originals.
Explicit eager compatibility views materialize complete bytes; normal
iter_batches() feeds ranges through the shared planner. Native frame metadata
still includes complete encoded/text payloads and needs further migration.

The common batch planner exposes incremental atomic-unit, group and batch
iteration. Existing eager callers collect that same iterator and retain their
preflight error timing. Incremental consumers must handle iteration failures
before dispatch or record them against complete durable work identity; the
installed run loop is not switched by merely exposing this interface.

Required citation association uses two replayable passes over immutable daily
records. The first retains only citation identity metadata across the whole corpus
so ambiguous repeated fragments remain ambiguous. The second validates references
with day-scoped byte/proof caches and checks ordinal identities against the first
pass. The completed association retains paths and diagnostics, not source bodies.

Archive batch planning feeds sequential daily records through the same atomic-unit
and group boundary algorithm. A group's measure owns only its current parts and
lookahead; context collections remain archive-backed. Required citations are built
globally first, and optional ranking excludes every pending daily source even when
that source is outside the current group. No installed dispatch boundary changes
until complete durable preflight and per-source failure accounting are integrated.

The retained independent capture helper first computes the full logical snapshot digest with the
common capture iterator, then writes through the queue's existing atomic artifact
writer and requires the same digest before publication. A source change refuses
the snapshot. A completed file whose event commit was interrupted is recovered
only after its manifest and every referenced record/blob validate against that
expected logical digest, under the existing owner/lease checks. No second staging
layout, database or temporary snapshot copy is introduced. This binds source
identity; complete work/dependency identity and installed routing remain separate
requirements before dispatch.
The queue's compile journal and artifact writers check the existing work's task
family as well as the current lease. Only the original task or its explicit redrive
lineage can append or recover progress. Capture requires the pending source identity
before its first snapshot transition; source identity alone is not a complete work
or dependency identity.
