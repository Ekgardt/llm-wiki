# Breadcrumb durability proposal

Current status, 2026-09-30: the qualified source changes described here are installed. See [the installation evidence](2026-09-30-durable-capture-installation.md) for the final regression, live capture and nightly results. The checkpoints below retain their original dates and describe the state at that checkpoint; their pending-installation statements are historical. Installation does not close the remaining warning review, every native-event qualification, or the wider limit audit.

### Semantic admission and producer cleanup checkpoint, 2026-09-30

The explicit 20-second comparison restored 3/10 old pages and 3/3 new facts
(median 8.66 seconds, maximum 16.60, 136411 response bytes). It is a diagnostic
alternative, not the selected default. A warm trace then showed nine expected
pages already among admitted candidates, at dense positions 1-9, but six below
the ten-item primary scoring prefix after prior weighting. The first cold trace
had not admitted dense work on three early questions; it was repeated with an
explicit warm-up and recorded signal availability, rather than interpreting
those absent candidates as retrieval quality evidence.

A private experiment admitting primary candidates by their original semantic
scores found 9/10 historical pages and 3/3 new facts at the unchanged 14-second
setting (median 7.36 seconds, maximum 8.64, 111727 response bytes). Only the
symlink-ancestor expected page remained absent from the candidate pool. This
exceeds the historic 0.6 expected-page gate on these ten questions, but is not
proof for every memory question or full-cycle model-answer/token efficiency.
The same admission correction is now implemented for primary-only and mixed
pools; no semantic score means stable prior ordering. Provenance still weighs
the final score, and exact/disabled bypasses remain. Two new primary-admission
regressions failed before the change. Unpatched product qualification is pending.

Fresh native generation before the batch: generation-18da2151efc5f58a-b0170c89,
zero stale paths. Graph and source searches showed the old direct append helpers
had only test callers, and capture_operation only those replaced wrapper helpers.
The candidate removes those implementations, content/time suppression and unused
preview/filter constants. Entry scripts and historical command arguments remain
for installed host configurations; v1 session readers remain for retained work.
Prior source bytes are preserved privately for review/rollback. Failure reporting,
forged-heading protection and writer-contention tests now exercise real durable
ingress/delivery instead of the retired direct append.

Cleanup also reproduced a false-loss report: after durable acceptance, an
auxiliary prompt-counter exception was counted as lost capture. The counter now
passes that exception to the existing accepted-follow-up boundary, which reports
it without claiming loss of the saved event. The new regression initially failed;
the counter/follow-up pair now passes. Broader cleanup checks are still running.
One adapted lock test initially expected RuntimeError; the actual documented
StateLockTimeout is now asserted specifically. No lock check was disabled.

### Product-path qualification failure, 2026-09-30

The unpatched candidate repeat at the shipped 14-second setting failed the
quality gate: 2/10 historical pages and 0/3 new facts. Unit-level admission,
ordering and failure protections passed, but this is not a deployable retrieval
qualification. The experimental comparison had an additional warm-up request;
its success must not be substituted for the product result.

A diagnostic recorded a completed full query at 12.52 seconds when explicitly
granted 30 seconds, including 3.32 seconds for primary scoring and 6.32 seconds
for event scoring. The same fact was then missed at 14 seconds because the
second stage could not fit with the existing tail reserve. The graph-based
citation question also lost primary scoring. These measurements establish an
insufficient window for this observed workload; they do not establish a universal
new default. A separate run of the existing configurable retrieval setting at
20 seconds is under evaluation. No shipped default or caller limit is changed.

Final mechanical checks passed 24 tests including CCN/branch shape, shared
capacity, secondary refusal, unchanged deadlines and disabled scoring; both
new test files passed 27 tests while their scheduling weights were measured.
The complete audit, model-answer token efficiency, legacy cleanup and installation
remain unfinished. [Google SRE service objectives](https://sre.google/sre-book/service-level-objectives/)
was rechecked on 2026-09-30: latency distributions and correctness must be judged
together. This supplements the three independent retrieval sources and deadline
references below; it is not evidence that this implementation is optimal.

### Source scoring correction checkpoint, 2026-09-30

The selected candidate now scores primary pages first and verified breadcrumb
evidence second, using the existing model and ten-item depth per pass. Event
admission uses its original dense score rather than a trust-weighted position
below every compiled page. Both passes keep the caller deadline, existing stage
ceiling and measured tail reserve. Separate observed costs share the same
cross-encoder semaphore: this does not increase concurrency or add a model.
A declined or failed event pass retains the completed primary answer and reports
its reason. Scored candidates merge by their existing comparable blended scores
with provenance weights; unscored candidates keep their original relative order.
There is no fixed source quota. Exact-title, disabled and notes-only paths retain
their existing behavior. This remains a candidate, not an installed change.

Private paired experiments on the same frozen corpus found 3/10 historical pages
and 0/3 real-ingress boundary facts before the correction. Separate cost accounting
with alternating output found 3/10 and 3/3, but increased response bytes from
107716 to 185874. That quota was not selected. Merging by existing scores found
3/10 and 3/3 with 136411 response bytes; median request time was 8.53 seconds
versus 4.65 seconds, maximum 9.20 versus 8.98, within the normal 14-second request.
These are thirteen retrieval questions, not a full-cycle answer/token benchmark
or proof of a generally optimal policy. The historical 0.6 note-quality gate
still fails at 0.3 and is not waived. An unpatched product-path repeat is pending.

Six initial regression checks failed and three controls passed. After the fix,
103 related retrieval/model/admission checks passed with three existing PyTorch
deprecation warnings. The first complexity gate rejected two new functions at
CCN 6 and 8; splitting source selection and trace measurements corrected them,
and the repeated regression/complexity group passed 20 checks. Four further
checks cover shared live capacity, unchanged deadlines, secondary refusal and
disabled scoring; their final combined result remains pending. Ruff passed.
Doctor's final reporting/rebuild group, including absence of a misleading
repair-deferred flag after failure, passed all 15 checks.

The fresh native code generation before this batch was
`generation-18da2036569a3cef-5d47c50f` (244.23 seconds, zero stale paths on detection).
Graph callers and direct source inspection covered the admission, deadline,
source-diversity and semaphore boundaries. The candidate's laws file was also
synchronized byte-for-byte with the already user-approved installed revision;
no law was rewritten. Research sources and rejected alternatives follow below.

### Repair reporting correction and bounded search experiments, 2026-09-30

The doctor reporting defect is corrected in the candidate. Repair-action names
are now mapped to their actual health checks using the existing action/check
map. Both generations/generation and indexes/claims failed the new regression
before the fix; queue/queue already worked. The report now retains the redacted
cause instead of claiming lock contention. The rebuild fixture owns an isolated
Git repository. Validation: 26 report/rebuild/complexity checks and 10 related
doctor checks passed; Ruff passed. This is not a claim about installed health.

The current same-corpus bounded comparison records 3/10 historical expected
pages and 0/3 new facts for the unchanged ranking. An interleaved candidate pool
with twenty scored items found all three facts without a deadline, but under the
normal request budget it scored 1/10 and 0/3: the optional stage timed out or was
not admitted. That variant is rejected. A diagnostic giving the final stage only
the remaining original deadline minus the existing 2.5-second tail reserve found
3/3 new facts within the normal request budget, but 2/10 old pages. The citation
question lost its previously found decision because its reranker was not admitted
after the graph stage; inspection of the returned pages confirmed that they did
not replace the missing answer. This is a real failed regression gate.

The next experiment keeps the primary-page result before attempting event
reranking, under the same request deadline and existing per-pass model depth.
It is not yet a selected product design. Corpus quality, token use of complete
answers and safe host installation remain unqualified. Existing historical
quality thresholds have not been weakened.

Research rechecked 2026-09-30: [Microsoft RRF](https://learn.microsoft.com/en-us/azure/search/hybrid-search-ranking)
explains rank fusion and the separate subsequent semantic score;
[Anthropic contextual retrieval](https://www.anthropic.com/engineering/contextual-retrieval)
supports evaluating chunk context and reranking on actual tasks;
[BAAI's model card](https://huggingface.co/BAAI/bge-reranker-v2-m3)
describes the multilingual cross-encoder. The actual experiments used the pinned
local model revision recorded in their traces, not an unpinned download. No new
provider, model or runtime dependency was introduced.

Deadline references rechecked the same day: [gRPC deadlines](https://grpc.io/docs/guides/deadlines/),
[Python monotonic time](https://docs.python.org/3/library/time.html#time.monotonic),
and [libcurl total timeout](https://curl.se/libcurl/c/CURLOPT_TIMEOUT.html).
These support elapsed-time accounting and a bounded overall operation; they do
not prescribe this vault's source allocation or prove that half of the remaining
time is the right allowance for its final stage. The retained tail reserve is
based on the vault's documented measurements. The AWS timeout article redirected
to a page without readable content and is not counted as evidence in this check.

### Subsequent verification, 2026-09-30

The three real-ingress boundary cases were delivered without a model, and an
incremental vector generation activated in 148.14 seconds (1847 sources, 352
rebuilt, 1495 reused). Ordinary semantic questions nevertheless missed all three
facts. In one trace the correct lexical chunk was first, but fusion placed it
40th and the ten-item reranker did not see it. The English fact appeared 127th in
the weighted dense output and 184th after fusion. Delivery is proven; useful
retrieval is not yet qualified.

Private diagnostic ablations are not installed: moving the existing coverage
step before reranking, removing the final source-kind grouping, and combining
those changes did not recover the three facts. Allowing coverage to replace a
unique but unselected source recovered the two same-language facts, not the
English fact. A twenty-item rerank with interleaved source classes likewise
recovered only two. This costs more scoring and has not passed paired quality or
latency gates. No ranking policy or rerank depth was changed in production code.
Some diagnostic runs shared CPU with index refreshes; their timings are not
controlled latency comparisons. Runs without a caller deadline test ranking
possibilities, not the bounded product contract.

The candidate's no-active-generation path now follows the same verified
breadcrumb corpus and returns canonical bounded physical chunks. It preserves
notes-only scopes, rejects incomplete evidence, and observes the caller deadline.
The old fallback failed the new large-tail test. The related corpus/search and
Lizard/AST group now passes 216 with three skips; Ruff passes. A separate doctor
rebuild test failed: diagnostics confirm inherited Git identity from /tmp in its
fixture, and reveal a reporting mismatch (repair error key generations versus
check key generation) that wrongly reports a held lock. These remain explicit
open findings, not a green full suite.

Two unreachable legacy adapter dispatch functions were removed after graph and
source-reference checks; 34 ingress and complexity checks passed. Other legacy
producer helpers remain and cleanup is not complete. All edits remain candidate
only. Native index generation-18da1d0b45bbe842-bbaae014 was fresh before this batch;
it requires refresh for subsequent edits.

### Linked-source retrieval qualification, 2026-09-30

Status: candidate implementation and experiments, not installed or closed.
Only complete, digest-verified breadcrumb heads and their referenced parts join
the existing corpus. Ordinary session dumps and orphan parts remain excluded.
Physical source bytes and citation spans are preserved; decoding an event does
not manufacture offsets into its Markdown representation. Missing or modified
parts refuse collection. Archive and occurrence-date policy remain explicit.

The selected prototype reuses the existing collector, FTS table, vectors and
retrieval pipeline. It adds no model call to event delivery and no second index.
The previously cited Anthropic, Microsoft and SQLite sources inform chunking,
rank fusion and corpus-sensitive scoring, respectively; they do not establish
that this prototype is optimal. A separate event index would add ownership and
consistency costs. Importing all session dumps is outside the accepted scope.

A reproduced candidate-admission defect allowed raw matches to evict a decision
before trust weighting could operate. The prototype retains each source class's
existing caller-derived candidate allowance before combining them, preserving
engine score order. It can therefore inspect up to twice as many candidates;
final answer and explicit candidate caps still apply. This is a measured design
tradeoff under qualification, not a free improvement or an arbitrary fixed quota.
Shared FTS statistics still change when sources are added.

Validation: 232 related tests passed, three skipped, including real Lizard/AST
complexity checks, verified physical citation spans, corruption refusal and a
large-event tail retrieved through the public retrieval/context path. Ruff passed.
The dense admission test uses controlled vectors only to prove ordering; it is
not model-quality evidence. The native code index was refreshed successfully in
213.98 seconds (generation-18da1c9b2a9b5eaa-9c23232f); detection then reported zero
changed files. The preceding producer Ruff rerun also passed.

Private paired experiments used the real cached multilingual-e5-small embedder
and bge-reranker-v2-m3, with offline model loading. The baseline contained 960
sources and 4663 chunks; 439 session-derived stress fixtures expanded it to 1838
sources and 11795 chunks. These fixtures are not newly captured user events.
Full vector builds took 110.92 and 672.29 seconds. The historical expected-page
hit@5 was 2/10 in both runs, below its historic threshold; expanded queries were
slower. That run predates the final candidate-order correction and must be
repeated. The old expected pages may have newer valid alternatives, so this
proxy alone does not establish answer correctness. Disabling coverage selection
in an isolated diagnostic did not improve the baseline; no such production
change was made. Context bytes were measured, not tokens.

Three boundary-size events subsequently passed real adapter ingestion and the
real adopted queue worker without a model call, including a Russian question's
English source. Search over these events is still being qualified. An initial
fixture setup failed because it lacked the installed adapter file required by
adoption; copying the actual candidate scripts corrected that setup. No adoption
check was disabled. Evidence: /tmp/audit-breadcrumb-retrieval-boundary-delivery-v2.txt.

Still open: current paired ranking and context-cost measurements; boundary-event
semantic retrieval; no-active-generation retrieval of linked events; producer
legacy cleanup; host qualification; safe installation and final regression.
This subsection supersedes earlier statements that the candidate collector still
excludes all raw session paths. The installed collector remains unchanged.

### Common producer routing qualification, 2026-09-30

Status: implemented in the isolated candidate only. The installed vault still
uses the previous producers; no live cutover or audit closure is claimed.

Prompt/tool ingestion now publishes its immutable breadcrumb before checkpoint
observation and project follow-up. Acceptance survives worker launch failure;
the existing worker accepts both v1 session and v2 breadcrumb handlers. Direct
prompt/tool wrappers use this same ingress. Short meaningful prompts and commands
are preserved. Explicit host replay retains its first acceptance, and conflicting
input under the same identity is refused. Auxiliary failure after acceptance is
reported separately from lost capture. The twentieth-prompt session capture and
tenth-prompt advisory remain, after durable acceptance.

The first integration run found fixtures expecting the retired delegate call
instead of durable publication. Those checks now inspect real stored input and
queue tasks. Two genuine regressions were reproduced and corrected: an empty
tool hook attempted publication; a canonical OpenCode tool input lost its target
because only native args/input fields were read. The latter failed for OpenCode
while Claude/Codex passed; the normalizer now accepts the common tool_input alias.

Validation evidence is under logs/audit-2026-09-30-breadcrumb-cutover- and the
original /tmp/audit-breadcrumb-cutover-* logs. One integration group passed 195
with 21 skips; the direct-hook/diagnostic/complexity group passed 73; the final
OpenCode correction group passed 107. The combined storage, recovery, worker,
normalization, hooks and actual Lizard/AST group passed 437 with 21 skips. The
combined process had loaded the adapter before the final alias correction; the
107-test run covers that final adapter change. These overlapping groups must not
be summed. Ruff passed before that last small alias correction and must be rerun.
All results are candidate checks, not host qualification or live health evidence.

The initial refresh produced generation-18da18e381f6c586-6c254be3. A subsequent
refresh hit its deadline during graph validation while the combined suite was
running. This is an unsuccessful refresh; remaining edits need a fresh index.
No complexity threshold, assertion, adoption refusal or storage check was disabled.
Old direct-writer helpers remain temporarily, with their legacy helper tests;
they require removal after common-path qualification and before final completion.

Still open: large-source retrieval integration and paired quality/cost evidence,
remaining host fault/deadline qualification, safe quiescent installation, removal
of replaced direct writers, final checks and index refresh. The normalized
breadcrumb is retained in full; this does not expand the existing host normalizer
into a recorder of arbitrary tool response bodies. Private wiki/log updates remain
deferred until operational ownership permits safe writes.

### Retrieval preparation and shared read deadline, 2026-09-30

The preceding corpus-membership probe preserved a large event byte-for-byte in
permanent Markdown, but collected only its daily reference. Its unique content
was absent from that corpus. A short inline event did contribute its content.
Evidence: `logs/audit-2026-09-30-breadcrumb-occurrence-corpus-membership.json`.
This establishes a retrieval preparation gap, not successful search qualification.

Blindly indexing the raw session tree is rejected: the existing project record
reports that 236 imported sessions reduced its retrieval stand from hit@5 0.7
to 0.0. Merely lowering trust or reordering an already selected pool did not
recover the missing candidates. That is historical project evidence, not a new
measurement. Current source still excludes that tree. A fixed source quota would
also require task evidence; no quota or relevance cutoff is introduced here.

Retrieval research checked on 2026-09-30:
[Anthropic contextual retrieval](https://www.anthropic.com/engineering/contextual-retrieval)
explains why chunk context and paired evaluation matter;
[Microsoft hybrid ranking](https://learn.microsoft.com/en-us/azure/search/hybrid-search-ranking)
describes rank fusion, which cannot recover an absent candidate;
[SQLite FTS5](https://www.sqlite.org/fts5.html) documents the existing lexical
engine and corpus-dependent ranking. None proves an optimal source-allocation
policy for this vault. Alternatives remain: importing all sessions, following
verified breadcrumb links as part of collection, or a separate retrieval pass
inside the existing search. Selection and integration require a paired check of
note retrieval, event retrieval, latency and useful context cost. No second
index, new tool, arbitrary quota or model summarization is installed.

One concrete prerequisite is now implemented in the isolated candidate. The
permanent-source reader previously accepted no caller deadline at all. Its
multi-part and archive reads therefore could not participate in a bounded search
request. `read_permanent_source` now accepts an optional absolute monotonic
`deadline`, validates it before path access, passes it unchanged to each bounded
physical read and archive fallback, and checks it after integrity reconstruction.
Expiration raises `TimeoutError`; it never returns partial evidence. Existing
maintenance callers retain their previous default because this change does not
invent a timeout for them. Search integration must supply its existing deadline.

The design reuses `bounded_io`, with no dependency, format, path, environment or
runtime-root change, and uses APIs compatible with the project's Python 3.10
floor. Primary references checked on 2026-09-30:
[Python monotonic time](https://docs.python.org/3/library/time.html#time.monotonic),
[gRPC deadline propagation](https://grpc.io/docs/guides/deadlines/), and
[libcurl whole-operation timeout](https://curl.se/libcurl/c/CURLOPT_TIMEOUT.html).
They support an overall budget instead of a renewed per-part allowance. A new
background thread/process was rejected as unnecessary for this shared cooperative
reader. A single blocking OS call cannot be preempted by these checks; this is not
a hard real-time guarantee and does not weaken the local-filesystem requirement.

The native graph was fresh before editing at
`generation-18da1687419b8f38-d101aca2`. Caller queries and source inspection covered
collection, evidence restoration, terminal proof, source publication and archival.
The existing live runtime permissions were rechecked as user-owned, directories
0700 and queue 0600. No production operational writes were performed.

New checks cover expired/invalid deadlines before access, one shared allowance
across multiple parts and archive reads, expiry after final integrity work, and
successful full-content reads while time remains. The initial focused run failed
seven cases because the API lacked the deadline parameter; its one passing type
error was not evidence of correct validation. The completed group passed 54 tests
in 49.78 seconds, including existing source publication, terminal/purge/archival
proofs and actual Lizard/AST complexity checks. Ruff initially reported import
formatting; formatting was corrected and Ruff passed. No check was weakened.

Evidence prefix: `logs/audit-2026-09-30-breadcrumb-reader-`. This is candidate-only
reader qualification. Search routing, paired retrieval/cost measurements, producer
activation, safe deployment and final legacy cleanup remain open. Private wiki
and log updates remain deferred until operational ownership permits safe writes.


### Occurrence identity qualification, 2026-09-30

Status: candidate-only correction; live producers remain unchanged. The native
repository graph was fresh at generation `generation-18da14c5090d57f4-ea96150c`
before editing. Caller queries covered normalization, ingestion, direct prompt
and tool entry points and durable publication. Missing graph connections were
checked against source and hook configuration. The live runtime remains owned by
the vault user (directories 0700, queue 0600); its read-only state reported the
last nightly as failed and compilation as running. This is not cutover consent
or evidence that old operational owners are quiescent.

A new real-publication regression reproduced two distinct equal host actions
with one timestamp collapsing into one durable intent. Prompt occurrences were
excluded from generated identities; timestamps prevented generated identities
for other events; and an empty generic identifier hid a valid tool-call ID.
Ten of the first thirteen cases failed before the correction. The candidate now
assigns an occurrence at ingress whenever a nonempty source identity is absent,
including prompts and events carrying timestamps. Empty aliases cannot hide a
later valid identifier. Reusing an already identified event preserves its ID;
two separate unidentified invocations stay distinct. Timestamp and text alone
are not evidence of a retry. No persisted format, runtime path or limit changed.

The common adapter still does not treat Codex `turn_id` as a prompt occurrence.
An intermediate change made that assumption and passed a test written with the
same assumption. It was removed before qualification: the official contract
names an active turn, not a unique steering-message delivery. The final regression
requires equal prompts in one turn to remain distinct without an explicit event
identity. Existing lifecycle-specific Codex mappings are outside this correction;
this is not host-level exactly-once delivery qualification.

Research rechecked on 2026-09-30: [AWS transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)
requires consumers to tolerate duplicate delivery;
[SQLite atomic commit](https://www.sqlite.org/atomiccommit.html) explains the
filesystem and synchronization assumptions behind durable completion;
[RabbitMQ acknowledgements](https://www.rabbitmq.com/docs/confirms) distinguishes
publisher acceptance from consumer completion. The additional
[official Codex hooks contract](https://learn.chatgpt.com/docs/hooks) distinguishes
turn identity from tool invocation identity. These are independent primary
sources, not proof of this implementation. Existing Python 3.10-compatible UUID,
JSON and v3 storage APIs are reused, with no new dependency. Content/time dedupe
was rejected because it conflates separate actions. Generating an ID when the
host supplies none can preserve a host redelivery twice; dropping a distinct
accepted action is the less acceptable tradeoff under the approved contract.

Final normalization/storage/envelope/Codex/integration and actual Lizard/AST
checks passed 169 tests, with 21 skipped, in 57.94 seconds. Ruff passed. The
skipped checks are not qualified. The earlier 85-test run included the rejected
turn-ID assumption and is not proof of the final change.

Three synthetic real-queue cycles measured publication, worker completion and
byte-exact recovery from permanent Markdown, with model invocation set to fail.
For canonical inputs of 50, 1,048,589 and 1,572,871 bytes, publication took
120.95, 570.05 and 1,097.93 ms; completion from publication start took 559.20,
1,873.47 and 2,612.14 ms. All restored bytes matched and model calls were zero.
The larger probes stress transport expansion using the adapter's existing input
bound; they are not claims that those expanded JSON objects fit that host's
stdin bound. These single local samples are not percentile/load guarantees and
do not qualify indexing, retrieval or whole-task token efficiency.

Evidence prefix: `logs/audit-2026-09-30-breadcrumb-occurrence-`.
Producer routing, host/maintenance activation, retrieval quality/cost, safe
quiescent deployment, final suite/security/platform checks and removal of the
replaced direct writers remain open. The private wiki/log update remains deferred
until operational ownership permits a safe write.


Status: selected under delegated authority; implementation in progress, producers
not activated. Research date: 2026-09-29.

The owner has since instructed the agent to make decisions based on all nine laws,
in direct reply to the implementation authorization question. The selected contract
is recorded in the private decision `durable-breadcrumb-delivery-decision.md` and
in `docs/STRUCTURE.md`. This delegates the choice; it does not establish that any
implementation or qualification gate has passed.

Revision: the first proposal was withdrawn from approval because its dependency,
identity, compatibility and limit analysis was incomplete. This revision separates
verified facts, the proposed contract and the evidence required after implementation.
It does not certify the current implementation against the development laws.

## Problem and evidence

Prompt and tool breadcrumbs currently call the Markdown writer from a hook.
If the writer gate remains occupied beyond the hook's deadline, no transaction
need have been prepared. The diagnostic counter then proves a loss but cannot
reconstruct the missing content. Longer waits reduce the probability and still
lose a record when the host terminates the hook. A rate-limit state record is
not a durable copy of the breadcrumb.

Sources in this repository: `daily_log_append.locked_append_once`,
`user_prompt_capture._append_prompt_tag`, `post_tool_capture._append_tool_tag`,
and `tests/test_a_tool_breadcrumb_waits_in_the_background.py`. The installed
vault's failure trail confirms writer-gate timeouts. Existing refused-append
repair recovers prepared after-images; it cannot recover a pre-preparation loss.

## Proposed contract

1. Persist the normalized, redacted prompt/tool event at the adapter ingress,
   before rate-limit state, checkpoint work or a subprocess delegate can fail.
   Use the existing fenced capture-intent storage and queue. No new daemon,
   database, runtime directory or MCP tool is introduced.
2. Introduce an explicit versioned breadcrumb record, distinguished from a
   session transcript. Preserve the first accepted occurrence time, source event
   identity, project, session, tool/target or prompt text, and integrity hashes.
   Redelivery must validate the same logical event and reuse its first record;
   it must not invent a new capture time or accept conflicting payloads.
3. Render the breadcrumb deterministically in the existing capture worker.
   No LLM is called to save a prompt/tool breadcrumb. Its processing receipt
   explicitly says deterministic; it cannot masquerade as a model response.
4. Append through the existing recoverable Markdown transaction boundary, using
   the original day and a stable operation ID. Only a verified terminal receipt
   linking to the committed transaction permits retirement of the intent.
   Enqueue success, worker startup or an attempted append is insufficient.
5. Retain session-capture v1 readers for existing session records and queued
   work. They remain necessary for that supported contract. Remove the replaced
   direct breadcrumb write path after migration and verification; direct hook
   entry points must call the same durable publisher.

This changes the persisted capture/receipt schemas and hook delivery contract.
The repository operating contract requires decision authority before implementation;
the owner's subsequent delegation is recorded above. Existing capture records must
remain readable throughout.

## Alternatives and tradeoffs

- Longer hook waits: already implemented for background tool hooks; they still
  cannot survive process death before persistence and consume host resources.
- An ordinary queue task alone: it does not satisfy the approved create-only
  evidence/terminal-proof contract when enqueueing or adoption is interrupted.
- A new service or database: unnecessary; the installed v3 protocol already
  owns durable admission, replay, integrity and terminal completion.
- Reuse the current session classifier: would add model cost and could classify
  away an operational breadcrumb; it would also mislabel the evidence.

Chosen proposal: extend the existing durable protocol with a typed deterministic
path. Cost: more small disk records and versioned validation code. Benefit:
writer contention delays delivery instead of destroying the only stored copy.
Disk/admission failures still must report a failed capture; no implementation
can guarantee persistence after the storage device has refused the write.

## Qualification before installation

Use the actual queue, ownership registry and transaction coordinator in temporary
vaults. Hold a second-process writer; terminate a producer after durable publish
and a consumer after commit but before terminal acknowledgement. After restart,
verify original content/day, one append, matching hashes and no LLM invocation.
Exercise conflicting duplicate payloads, midnight redelivery, DLP refusal,
admission refusal, failed worker spawn, legacy session records and incomplete
adoption. An intent must remain available whenever terminal proof is absent.
Measure foreground publish latency against the existing host deadline.

## Current primary research

- [AWS transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html):
  persisting the event before delivery avoids the dual-write loss window;
  consumers must tolerate duplicate delivery.
- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html): durability
  relies on the journal, synchronization and filesystem assumptions. Keep the
  project's rollback-journal/FULL/local-filesystem contract.
- [RabbitMQ acknowledgements](https://www.rabbitmq.com/docs/confirms): publisher
  confirmation and consumer completion are separate facts. Apply that distinction
  locally; RabbitMQ itself is not a proposed dependency.

No new technology version is needed. The implementation must remain Python 3.10
compatible and use the installed v3 ownership/capture protocols. These sources
support the reliability principles, not a claim that this unimplemented proposal
has already passed qualification.

## Rechecked system and dependency boundary

Codebase Memory was fully reindexed on 2026-09-29: 50,509 nodes, 230,796 edges,
no partial parses reported. Connected graph queries covered the adapter entry,
prompt/tool writers, capture publisher, queue consumer and daily append. Private
knowledge and runtime paths are excluded from that graph; source inspection and
the running system supplied those facts instead. An index is not runtime proof.

The paths checked are:

| Stage | Current implementation | Change required |
| --- | --- | --- |
| Ingress | `normalize_occurrence_event`, `ingest_event`; checkpoint observation precedes dispatch | Publish accepted breadcrumb evidence before checkpoint/delegate work |
| Direct entry | `user_prompt_capture._record_prompt`, `post_tool_capture._capture_tool` | Route through the same publisher, without recursive delegate invocation |
| Existing suppression | `capture_operation` rate reservation precedes append | Do not suppress distinct accepted occurrences merely because their content matches |
| Admission/publication | ownership registry, intent fence, create-only pending/ready files, indexed task binding | Reuse these checks; never bypass maintenance or reuse another owner's fence |
| Recovery | `capture_adoption` and installer pending-file recovery | Recognize the new schema and handler before a new writer can publish it |
| Processing | `flush_memory.process_new_capture` requires a session classifier | Add explicit deterministic dispatch, without a synthetic `LLMResult` |
| Completion | decision digest, transaction binding, terminal receipt, purge validation | Validate the deterministic receipt and committed append before retirement |

The installed system uses a user systemd service and the existing native hooks;
there is no persistent capture daemon. The inspected runtime directories are
owned by the current operating-system user and have mode 0700; the inspected
queue path has mode 0600. `doctor` confirms adopted ownership and local locking.
These permissions do not isolate mutually hostile programs running as that same
user. The operating contract names the repository owner as the decision authority;
no separate CODEOWNERS allocation was found. Code changes belong to this project,
not to the agent hosts.

The runtime check still reports historical capture loss and a failed nightly
run. Queue/transaction checks are currently healthy, but retained failures and
quarantine history exist and are not deletion permission. This proposal neither
repairs historical missing input nor makes those counters disappear.

## Identity, time, ordering and failures

The persisted record must separate occurrence identity from payload integrity.
A digest of the payload is not an occurrence identity: two equal prompts can be
two real actions, while two different payloads claiming one occurrence conflict.
Scope source identity by host, session, event kind and worktree. Persist the full
redacted semantic payload digest separately. A repeat must compare that digest
against the original before reusing an intent. A conflicting repeat fails visibly
and cannot overwrite either the accepted record or its completion proof.

Use a host-provided event/tool-call identifier when available. Codex documents
`turn_id` for prompts and `tool_use_id` for tool calls; the present generic
extractor does not use `turn_id`. Claude's documented prompt payload does not
promise an occurrence identifier. Without one, generate an identifier once at
ingress and carry it through all internal retries. A new host invocation without
an identifier cannot be proven to be a retry; preserve it as a distinct event.
Do not promise host-level exactly-once delivery or guess from a time window.
Sources: [Codex hooks](https://learn.chatgpt.com/docs/hooks),
[Claude hooks](https://code.claude.com/docs/en/hooks).

Preserve the host's valid occurrence time, or the first acceptance time with its
origin explicitly marked. Also persist the chosen daily-log date, so a restart,
timezone change or delivery several days later cannot change the target. Do not
reuse the current daily writer's two-day search as the deduplication authority.
Replay uses the immutable plan, operation identifier and transaction binding.
Concurrent workers may commit in a different order from event occurrence;
timestamps describe occurrence, while append order describes delivery. There is
no unverified global ordering guarantee across hosts.

There are three distinct outcomes: not accepted (validation, DLP, storage or
admission failed), durably accepted but pending, and committed with verified
terminal proof. A successful process exit used to avoid disrupting the host is
not capture success. Expose the actual outcome through the existing diagnostic
and health mechanisms. A worker-launch failure after durable publication is
pending work, not lost work. Disk-full or failed synchronization before durable
publication is a capture failure, not queued success.

## Compatibility and limits

The measured local runtime is Python 3.12.3 with SQLite 3.45.1. The supported
project floor remains Python 3.10; it is in security support until October 2026,
so this change must not depend on 3.11+ APIs. Revisit the floor separately before
upstream support ends. This is not a claim that every installed dependency has
been security-audited. Source:
[Python version status](https://devguide.python.org/versions/).

Keep rollback journaling, FULL synchronization and the local-filesystem contract.
The SQLite source above explains the filesystem assumptions; merely calling a
write API is not power-loss durability. No broker, network service or additional
package is needed. OpenCode remains a plugin adapter, with delivery contracts
qualified separately from Claude/Codex:
[OpenCode plugins](https://opencode.ai/docs/plugins/).

The existing v1 envelope permits only session events. Its size and shape checks
also occur in the queue, recovery code and installer. Therefore adding an enum
value only at the producer is not a compatible migration. Keep v1 parsing and
its interpretation intact; introduce a distinct versioned breadcrumb envelope
and deterministic receipt, with explicit handler-version dispatch. Unknown
versions must remain retained and report unsupported, never be classified as
an empty session. Activate readers, recovery and receipt verification before
enabling the writer; quiesce old workers through the existing maintenance fence.
Rollback must stop the new writer and retain new records until a capable reader
returns. An old binary is not a safe consumer of those records.

Do not inherit the current 30/60-second content suppression or 140/100-character
previews as evidence-retention rules. Preserve the accepted redacted breadcrumb
payload, and treat any display shortening as presentation with a link to its
complete evidence. The current queue admits at most 1 MiB per intent, and v1 has
additional field/chunk ceilings. Those are verified compatibility constraints,
not measurements proving the right limits for the new format. Before enabling
the writer, measure serialized size and publication latency across actual host
payload shapes, Unicode/escaping and boundary-size cases. If the supported host
input cannot fit losslessly, the format/reader contract must be revised first;
silently truncating it or inventing a new total-input cap is prohibited.

No new retry count, source-count ceiling, retention period or delay is selected
here. Existing admission/worker budgets and adoption-pass limits must be checked
under breadcrumb volume: the old assumption of roughly ten session intents per
day does not justify a high-frequency tool stream. Capacity work may defer a
persisted event, never silently discard it. Publication must fit the configured
host deadline; report measured headroom and backlog drain behavior, rather than
asserting a universal millisecond target. Any necessary deployment-dependent
limit needs its measured/contractual basis, consequences and review condition.

## Publication qualification and the boundary-size correction

`tests/test_capture_publication_with_busy_writer.py` uses a separate real process
holding the Markdown writer gate, the real v3 queue/coordinator, durable file
publication and a real queue claim. All three cases pass. On this machine, during
the full regression run, publication of 743 / 9,734 / 900,734 bytes took
0.074309 / 0.075390 / 0.093190 seconds. This measures the existing publisher only,
not host startup, normalization or the proposed end-to-end path. It establishes
that the capture publisher is not serialized behind the Markdown writer. It is
not a throughput qualification or an upper latency bound.

A synthetic prompt input of exactly the adapter's accepted 1,048,576 bytes
produces a 1,049,271-byte v1-shaped capture record: metadata adds 695 excess bytes
in this fixture. Therefore simply redirecting breadcrumbs into the existing
single-record encoder fails at a supported boundary. The previous proposal did
not account for this. The installed retained records also range up to 977,392
bytes, so assuming every capture is a tiny line is not supported by the vault.

Alternatives for this concrete boundary:

- Truncate the prompt or target: rejected because it destroys evidence.
- Increase the shared cap to another round number: rejected because it changes
  every reader's resource contract without establishing a sufficient bound.
- Separate a complete event into integrity-linked bounded records: selected for
  the new format. The per-record size remains the existing transport constraint;
  it is not a new limit on the logical event or an arbitrary maximum part count.
  Cost: additional files, fsyncs, recovery and cleanup cases. This cost is required
  by the demonstrated mismatch, and must be measured before live activation.

The proposed v2 protocol uses a small occurrence manifest plus create-only parts
under the existing capture-intent layout. A part identifies its occurrence,
position and predecessor digest; the final manifest names the last part, actual
part count, total byte length and full canonical-input digest. Linking parts
instead of embedding a growing list keeps the manifest independent of payload
length. Partition the canonical redacted envelope by actual encoded record size,
including escaping and metadata. No fixed shrink-attempt count or eight-part
ceiling is carried into this format. The original complete input is reconstructed
and verified before rendering or committing anything.

Persist the occurrence's first-acceptance metadata under its occurrence fence;
parts are immutable and cannot be substituted between occurrences. Only a
complete, verified manifest can become queue-ready. Part records are never
independent session tasks. Pending recovery, doctor, installer adoption and purge
must understand this distinction before the writer is enabled. Interrupted
publication leaves incomplete evidence retained and reported, not a fabricated
complete capture. Acceptance is acknowledged only after every required part and
the manifest have been durably published. A failure before this boundary remains
a failed acceptance; the protocol cannot recover bytes never persisted by any
producer.

The single terminal proof binds the complete manifest/input digest and committed
transaction. Cleanup traverses and validates exactly that occurrence's parts;
it cannot delete a shared, mismatched, incomplete or unproven record. The
retention/rollback contract still applies to the whole occurrence. Test missing,
reordered, duplicated, foreign and tampered parts; crashes between every part
publication and manifest activation; and recovery/purge of mixed v1/v2 storage.

The cleanup review identified a further requirement before implementation: runtime
parts cannot be the only full evidence after a journal entry contains a shorter
reference. `run/` is operational state, not a knowledge source. Publish a complete,
immutable, integrity-linked Markdown copy under the existing private
`knowledge/raw/sessions/<date>/` tree before terminal completion. The journal links
to that permanent evidence; terminal verification binds its contents as well as
the committed journal operation. Missing or changed permanent evidence blocks
cleanup. The existing common Markdown-reader bound applies to each source record,
and partial permanent publication remains retained evidence until recovery finishes.
No new top-level directory or silent truncation is justified by this requirement.

This is a local application of complete-object publication and digest-linked
objects, not adoption of S3, its service limits, automatic part expiry or Git
operations. Primary references checked on 2026-09-29:
[S3 multipart completion and full-object checksums](https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpuoverview.html),
[Git content-addressed objects and references](https://git-scm.com/book/en/v2/Git-Internals-Git-Objects).
The protocol details above are a project-specific design inference. Its crash
safety and cost still require the explicit qualification matrix; the sources do
not prove the unimplemented code correct.

## Evidence required by each development law

| Law | Evidence available at proposal stage | Gate before accepting the change |
| --- | --- | --- |
| 1 — Codebase Memory | Fresh graph plus source, permissions, ownership and live-health checks described above | Refresh after edits; inspect every newly reached reader, recovery path and integration |
| 2 — Current research | AWS, SQLite, RabbitMQ; host contracts and Python support, retrieved 2026-09-29; alternatives compared above | Re-research if implementation changes these choices; no unsupported claim of universal superiority |
| 3 — Honesty | Facts, proposed behavior and outstanding qualification are separated | Report failed/skipped checks and actual delivery outcomes; no completion claim while a gate remains open |
| 4 — Quality and cost | Reuse durable admission and transactions; breadcrumb rendering needs no model | Measure whole-cycle latency, disk/backlog cost, replay correctness and downstream compile/token effects on paired workloads |
| 5 — Complexity | Current portable Python/JS/PowerShell/shell gates run successfully | Run actual CCN and branch-shape analysis for every new/changed function; CCN <= 5, <= 2 ifs and <= 2 nesting levels, no if/elif chains or complex ternaries |
| 6 — No workarounds | No longer wait, fake classifier, success stub or weakened fence is the chosen fix | Exercise real failure boundaries; inspect that every entry uses the common publisher and every success has evidence |
| 7 — Root cause | Source trace and real writer-contention test reproduce timeout before publication | A regression must fail on the original publisher and pass after restart/replay; also cover prompt, tool and direct entry |
| 8 — Cleanup | Replacement scope and compatibility consumers are identified | Remove the direct-write/rate-reservation route after verified cutover; prove remaining v1 readers are required by supported sessions or retained work, then repeat checks |
| 9 — Justified limits | Host deadlines and current serialized-protocol constraints are identified; unjustified inherited assumptions are rejected | Complete capacity/size measurements before enabling writes; document every retained/new limit and its reconsideration condition |

The regression matrix must include: crash after pending-file fsync but before
indexing; after ready publication but before enqueue; after enqueue but before
worker startup; and after Markdown commit but before acknowledgement. Restart
recovery must find each accepted event without the producer's memory. Use process
coordination at the actual boundary, not sleeps chosen to hide a race. Also test
two simultaneous producers of one event, conflicting payloads, equal distinct
events, absent host identifiers, replay after several days, invalid dates,
changed timezone, Unicode/JSON expansion, storage exhaustion, failed fsync,
maintenance admission, DLP rejection, lease loss and mixed old/new records.

For the original failure, hold the real writer gate beyond the hook deadline:
the hook must leave recoverable evidence even though no daily append completed.
After releasing the gate, the real worker must commit exactly one operation for
that accepted identity and produce a validated terminal receipt. Assert zero
model invocations for this path and use the real queue/transaction APIs. Then
run relevant recovery/installer/integration regressions, the full suite, static
analysis and an installed-vault health check. A passing test of the old low-level
timeout alone is root-cause evidence, not proof of the new protection.

The complete path is not yet implemented or installed, so laws 4–8 have execution
gates that cannot honestly be marked passed in this document. Size/capacity
qualification under law 9 also remains open. These gates are mandatory work,
not exceptions to the laws. Until they pass, do not call the repair complete or
enable a new writer against the live vault.

## Implementation checkpoint, 2026-09-29

The pure linked-record protocol and fenced storage now exist. Seventeen protocol
tests cover complete UTF-8 reconstruction, escaping, boundary-size partitioning,
identity/time binding, and missing, reordered, foreign or damaged records. The
combined storage, queue, common-runtime-file and publication checks passed 98
tests with three platform-dependent skips. Eleven Python complexity/branch-shape
checks passed at that checkpoint. These results do not qualify the full worker.

The tests exposed two concrete integration details. First, the common runtime
reader mislabeled a missing final file as a containment violation. Its regression
failed before correction; absence now keeps `FileNotFoundError`, while out-of-root
paths and symlinks remain refused. Second, queue claims were restricted to handler
version 1. They now accept an explicit supported-version selection and still
default to version 1. A legacy caller therefore cannot claim a new breadcrumb.
This is stronger compatibility protection than assuming all old workers would
claim version 2; that earlier assumption was corrected by inspecting the query.

Publication now removes its pending manifest only after replay-safe enqueue.
This keeps disk discovery possible across both the pre-index and pre-enqueue
crash windows. Recovery scans unfinished manifests, not the entire history of
accepted events. It validates the complete bundle and retains damaged or unknown
records with a reported reason. Its host/maintenance activation remains pending.
The actual ownership rule requires both expired lease and OS-proven process
death; process-crash qualification waits for the stored lease deadline rather
than weakening that rule or fabricating dead owners.

Permanent complete Markdown evidence, deterministic worker receipts, terminal and
purge verification, ingress routing, live cutover, capacity and full-cycle token
measurements remain mandatory unfinished work. Passing storage tests does not
prove these requirements, and no live prompt/tool producer uses this format yet.

### Permanent-source and deterministic-receipt checkpoint

Complete permanent Markdown sources now have a codec, a contained bounded reader,
and publication through real owned Markdown transactions. Publication checks the
sealed capture binding and full input hash, rechecks the current shared DLP
policy on the complete input, writes immutable parts before the head, and verifies
reconstruction afterwards. Existing conflicting source bytes are refused. Tests
exercise recovery after a partial publication and reading without runtime parts.

The first transactional test exposed a path mismatch: repeating the occurrence
hash and part hash in one permanent filename exceeded the existing 128-byte
Markdown component contract. Permanent parts now use their full content digest
alone; that digest already covers the occurrence ID and position. No digest is
shortened and no storage check is relaxed. The runtime format remains unchanged.

The deterministic receipt has a distinct `breadcrumb-decision/v1` format with no
provider or model response. It binds full input, permanent source and the original
day's append plan. A small canonical event fits whole in the journal block; if
the resulting receipt exceeds the established 1 MiB transport, its journal block
links to complete permanent evidence. That is a measured serialization decision,
not a new logical-input cap or a truncation rule. Noncanonical input objects are
refused before rendering so quoted user text cannot become journal structure.

The combined source/receipt/protocol/DLP run passed 45 tests; two further canonical
input cases bring the receipt file to six passing tests. Permanent publication
and codec changes passed eleven Python complexity/branch checks before the latest
DLP refinements; the final combined complexity run remains required. These are
component checks: deterministic worker dispatch and terminal/purge verification
are not implemented or activated yet.

Delayed replay inspection also reproduced duplicate appends after normal undo
pruning. The common correction and its red/green evidence are documented in
`2026-09-29-pruning-undo-does-not-repeat-a-committed-append.md`. Runtime-root
overrides, archived journals, long publication leases and purge/restore of the
new auxiliary files remain explicit qualification cases before cutover.

### Completion gate qualification (2026-09-29, before activation)

Primary sources rechecked today: [AWS transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)
requires idempotent consumers; [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html)
explains the durable database boundary; [RabbitMQ acknowledgements](https://www.rabbitmq.com/docs/confirms)
distinguishes publication from completed consumer processing. These support checking
the actual destination before acknowledging this two-store workflow. None proves
this implementation; real fault and recovery tests remain required. The existing
SQLite rollback-journal/FULL contracts and pinned dependencies remain unchanged.

For handler 2, both completion and subsequent retention/purge inspection must
verify the exact manifest, deterministic receipt, complete permanent source and
committed journal transaction. The journal may have later appends; retained
before/after hashes prove its original prefix. A foreign transaction or unbound
receipt cannot grant completion. Handler 1 retains its existing contract; unknown
handlers fail visibly. Pass the known vault root through the queue factory so a
separate runtime root cannot be mistaken for the knowledge destination. Missing
vault context must refuse verification, not guess. This is in-process context,
not a new environment, database, directory or runtime contract.

Trusting terminal JSON alone does not prove publication. Adding another ledger
duplicates existing transaction authority. Keeping undo images forever increases
retention without proving current source integrity. Reuse the existing committed
transaction and full permanent source instead; this costs source/journal reads
at completion and before cleanup. Archived journals and auxiliary purge/restore
remain required before live cutover.

The receipt-size presentation heuristic remains provisional. Actual compile
packing of synthetic events produced 5 batches for approximately 50 KB and 56
for 900 KB; a larger reference-only record produced one. The compiler splits
large entries, so these measurements do not demonstrate a context-window refusal.
They are not billed LLM token measurements. The collector explicitly excludes
raw sessions (`corpus_snapshot._walk_knowledge`), so replacing journal content by
a link has not demonstrated equivalent retrieval. No producer activation or
whole-cycle efficiency claim follows from these component results.

The first completion gate qualified 44 checks including legacy captures and
complexity. Ten destination checks then covered distinct vault/runtime roots,
missing vault context, journal growth after append and pruned undo images.
Cleanup initially exported only the manifest and receipt: two regressions proved
the anchor and parts were omitted. After extending the existing export artifact
list with an explicit `source` kind, 41 checks passed, including restart after
manifest removal. One intermediate run failed on a missing constant import;
that failure is retained separately and is not counted as a pass.

Related consumers require attention before activation: `archive_sessions` moves
every old session-directory file; `episode_consolidation.session_records` treats
every Markdown file there as a session. Preserve the existing archive operation
and resolve each immutable breadcrumb record by its canonical logical path,
falling back only on absence to the existing month/day archive location. A
present conflicting file must never fall back to an older copy. Keep archive
provenance visible in the journal. Session consolidation must not classify
integrity fragments as independent conversations; the explicit breadcrumb
filename contract distinguishes those records. This does not establish whether
the journal's provisional presentation is sufficient for downstream retrieval.
Keeping permanent duplicate hot copies or disabling archival would accumulate
unnecessary state; changing the storage root would discard the approved layout.
The selected compatibility work reuses the same sources and archive, with no
new age, size, number-of-files or input-length limit.

### Delayed replay and redrive (2026-09-29)

A real archived-journal replay reproduced a duplicate: the general append API
intentionally recreates a missing file (needed by rotating header callers), so
a breadcrumb whose journal was archived before acknowledgement recreated the
flat day. Handler 2 now first locates its exact committed operation family and
proves the binding and journal bytes, including a validated archive. It reuses
that transaction; an absent or conflicting destination is an error. No numeric
search cutoff or second ledger is added. Thirty related checks passed.

Redrive qualification then tests the crash after an immutable terminal file was
written but before its task row was completed. A new task has a different binding,
while the one-per-intent terminal correctly names the predecessor. Rewriting that
file would destroy immutable evidence; repeating a model or append would repeat
completed work. The intended correction is exact ancestral adoption: prove the
terminal's sealed predecessor in the real redrive chain, require identical intent,
digest and handler version, verify its receipt and destination, and atomically
seal the child's receipt inheritance with the child's completion. Both tasks
retain their history and the terminal bytes remain unchanged. Unrelated tasks,
foreign bindings and invalid evidence remain errors. Existing retention keeps
shared redrive families; this work does not authorize their deletion.

Primary research rechecked on this date: [AWS redrive](https://docs.aws.amazon.com/step-functions/latest/dg/redrive-executions.html)
preserves successful work and history; the earlier SQLite atomic-commit and
RabbitMQ acknowledgement references establish the separate commit and delivery
boundaries. This is a local application of those principles, not a guarantee
supplied by those products. No AWS service, dependency or vendor quota is adopted.
For the pending publication-lease work, [etcd's lease API](https://etcd.io/docs/v3.6/dev-guide/api_reference_v3/)
and [Kubernetes leases](https://kubernetes.io/docs/concepts/architecture/leases/)
confirm renewal of live authority; the existing project's fenced expiry and
shared busy-retry mechanism must remain enforced, rather than raising a TTL or
letting a lost publisher report success. Publication renewal is not implemented
by this research paragraph.

### Qualified worker checkpoint (2026-09-29)

Exact ancestral terminal adoption is now implemented. The focused redrive run
passed 43 checks; seven additional binding/retention checks passed. Publication
now renews both its capture owner and intent fence. Worker renewal covers the
whole processing interval, including source publication and terminal proof,
instead of only the model call. Two failing renewal tests reproduced the missing
coverage before the fix. The combined run passed 134 tests in 211.11 seconds,
including actual default lease expiry, crash recovery, legacy capture and local
complexity checks. This is component qualification, not activation or full audit
closure. Proof: `logs/audit-2026-09-29-completed-repair-breadcrumb-combined-worker-weights.txt`.

Database recovery still selected handler 1 for every record. Four regression
tests reproduced wrong dispatch and acceptance of incomplete handler-2 sources,
at both pending and ready publication boundaries. Recovery must derive the
handler from a validated format, prove its identity against the index, and read
the complete linked source before dispatch. Unsupported formats remain retained
with a reported error. This reuses the existing format readers, queue, and
publication sequence; it introduces no storage or environment contract.

Indexed recovery qualification passed 35 tests. Two further regressions proved
that neither the worker nor nightly recovery discovered a completed manifest
left before database indexing. Both callers now use the shared physical recovery
pass before their indexed passes. The report counts recovered disk manifests,
including manifests that already had a row. The existing indexed batch limit
does not truncate this disk scan; its scaling with retained auxiliary files
still needs qualification before producer activation.

The integrated run passed 46 tests in 45.21 seconds, including legacy recovery,
worker delivery, nightly entrypoint and actual local complexity analysis. The
previous attempt passed those 46 tests but failed the session isolation guard:
the agent appended the private progress page concurrently with the test run.
That attempt is retained as failed; the isolated rerun kept the guard enabled
and made no concurrent knowledge writes. Proofs are retained under
`logs/audit-2026-09-29-completed-repair-breadcrumb-recovery-sweep-*`.

### Whole-source health and recovery traversal (2026-09-29)

The installed runtime validator checked the manifest bytes but not its linked
source. Three regressions reproduced missing/changed parts and a missing anchor
passing that check. It now shares the format/identity/full-source validator with
adoption and observes the doctor's existing caller deadline between bounded
record reads. Doctor's queue check also uses this proof and reports a redacted
cause when it fails. The integrated run passed 215 tests, with three platform
skips; actual Windows/macOS qualification is not inferred from this run.

Research for recovery traversal rechecked the following independent primary
sources: [Python 3.12 filesystem APIs](https://docs.python.org/3.12/library/os.html#os.scandir),
[SQLite row-value pagination](https://www.sqlite.org/rowvalue.html#scrolling_window_queries),
and [AWS transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html).
The deployed interpreter is Python 3.12.3 with SQLite 3.45.1; the product still
supports Python 3.10. The selected APIs and row comparisons predate that minimum.
No package or operational schema change is needed. SQLite rollback-journal/FULL
and the idempotent fenced publisher remain the durability authority.

Local filesystem measurements enumerating only retained auxiliary files took
approximately 1.1 ms for 1,000, 9.1 ms for 10,000 and 43.1 ms for 50,000. These are
single synthetic runs, not a production latency guarantee or a new supported
size limit. They do not justify moving existing auxiliary paths. The same
experiment showed `Path.glob` returning no entries for an inaccessible shard
while `os.scandir` raised `PermissionError`; regressions now exercise a complete
pending record behind inaccessible root/shard permissions. Use explicit shallow
directory iteration, report inaccessible directories and refuse symbolic-link
directories. Keep other shards recoverable. Silently accepting an empty result
would mask lost work; relocating files would not fix that cause.

The indexed sweeps still need qualification beyond their 256-skip prefix.
SQLite keyset pagination is preferable to repeatedly widening an oldest-first
prefix: it advances past failed rows without OFFSET rescans or a new persisted
cursor. A per-pass successful-dispatch batch is distinct from a limit on which
failed records are inspected. No source deletion follows from a failed scan.

Both indexed sweeps reproduced permanent starvation with 257 missing older
records. They now advance by `(updated_at, intent_id)` through keyset pages;
the 256-skip cutoff and widening-prefix implementation are removed. A test that
expected the old cutoff was replaced by stronger assertions that the good record
is adopted and every bad record is still reported. The associated run passed 43
tests; 15 boundary checks then covered equal timestamps, invalid cursors, both
publication states and filesystem scanning. No new database index was added:
the current schema has no matching compound index, so this avoids increasing
returned prefixes but does not promise an indexed query plan. A schema migration
is not justified by these measurements alone.

Successful adoption measured 0.038/0.260/1.011/2.114 seconds for 1/8/32/64 records
on this host. Retain the existing configurable default of 32 as a scheduling
tradeoff: approximately one second of recovery publication before processing a
queued capture in these measurements. It is not a data limit or a guaranteed
deadline; revisit it with storage latency/backlog measurements. No failed-row
count limits source reachability. Source: `/tmp/repair-capture-adoption-timing.json`.

Health must also distinguish durable but unindexed evidence from an empty queue.
Reuse the same strict shallow filesystem visitor for read-only inspection and
recovery. Inspect unindexed identities once per pass, report complete pending
records as pending, incomplete publication as unresolved, and malformed or
unreadable evidence with its cause. Indexed records already receive full source
validation. Respect the existing caller deadline, do not acquire publication
authority or enqueue during inspection, and retain every source. This extends
the existing reader/diagnostic contract; it adds no runtime path, database,
background process or ingestion authority.

The complete/unindexed and unsafe-directory health regressions passed after the
shared read-only visitor was connected. An incomplete-publication test initially
used a RuntimeError fixture while expecting OSError; it did not reach its health
assertion. That setup was corrected to inject an actual OSError at the manifest
write. The corrected integrated run passed 214 tests with three platform skips.
The physical recovery pass also previously ignored valid v1 session files before
indexing. A regression proved the missing dispatch; format validation now selects
either supported handler and both use the existing fenced publisher. Invalid v1
documents and unsupported formats remain reported and retained. Forty-four checks
passed, including real publisher termination and lease expiry. Directory traversal
now reuses the publication boundary's containment/reparse validation. Another
regression found that empty directories bypassed the inspection deadline; the
shared deadline check now covers directory enumeration as well as part reads.

Concurrency research additionally checked [SQLite rollback-journal locking](https://www.sqlite.org/lockingv3.html)
against the deployed 3.45.1 runtime. A streamed cursor across more than one batch
can retain a shared lock while full source files are verified, preventing another
connection from committing. Keep bounded metadata batches but finish each indexed
SELECT before reading source files; continue by the existing primary intent ID.
Do not switch to WAL, hold an unbounded materialized table, suppress a writer
error or extend its busy timeout. This uses the existing measured operational
metadata batch size, not a total-row limit. Normal health is a concurrent
observation; the separate maintenance fence still governs a quiescent snapshot.

The combined recovery/health qualification passed 371 tests with three platform
skips in 251.39 seconds. All 1095 source hashes matched its starting snapshot.
Ruff passed. The preceding run had one obsolete stage-order assertion: direct
observation proved that the physical pass delivered the legacy session exactly
once before the indexed pass. The replacement assertion checks that delivery and
the absence of another task. Both failed and successful evidence are retained in
`logs/audit-2026-09-29-completed-repair-breadcrumb-health-recovery-*.txt`.

### Renewal shutdown qualification — 2026-09-29

Four synchronized regressions reproduced authority scopes ending while their
renewal thread was still running: worker and publisher, on normal and exceptional
exit. A timed join neither cancels nor proves completion. The publisher detected
this only on successful exit; raising there still allowed outer scopes to release
authority underneath the thread. The worker did not detect it at all.

Primary sources checked on 2026-09-29:

- [Python 3.12 threading](https://docs.python.org/3.12/library/threading.html#threading.Thread.join):
  joining without a timeout waits for termination; a timed join can return with
  the thread alive. Threads cannot be forcibly stopped through this interface.
- [SQLite busy timeout](https://www.sqlite.org/c3ref/busy_timeout.html):
  contention waiting is attached to a connection operation, not the lifetime of
  the Python renewal thread. Multiple calls and OS scheduling are not covered by
  one such timeout.
- [Kubernetes leases](https://kubernetes.io/docs/concepts/architecture/leases/):
  heartbeats update time-limited ownership. This supports preserving the existing
  renewable authority model, not releasing it while its owned work continues.

For Python 3.10-compatible local threads, select cooperative stop followed by
actual join before releasing the surrounding fences, on both exit paths. Keep
publication's existing failure and live-authority checks. Increasing a guessed
timeout still permits the race; merely reporting expiry does not stop a live
thread; a process-based renewer would add an unjustified lifecycle boundary.
Tradeoff: shutdown waits for an in-flight SQLite operation, and an OS-level stuck
I/O operation can delay it. SQLite busy limits are not an OS I/O deadline. No new
timeout, daemon, dependency, schema, runtime path or lease policy is introduced.
This does not yet qualify the entire worker under all storage faults.

The capture shutdown batch passed 32 tests, including actual complexity analysis.
The earlier general-queue policy in
`2026-09-11-a-join-has-a-bound-and-a-benchmark-does-not-grade-itself.md`
uses bounded joins plus named refusal; it remains unchanged. The capture-specific
choice above concerns nested scopes that release their authority on either exit.
Other ownership and writer shutdown paths still require broader qualification;
the result does not establish that every renewer has safe shutdown ordering.

### Prompt input read failures — 2026-09-29

Graph and source tracing found `_read_stdin` had only the prompt parser as a
caller. It swallowed every read exception and returned empty text, bypassing the
existing `main` failure recorder. A closed real text stream reproduced a silent
loss in the prompt hook; the tool hook already recorded it. Remove this wrapper
and let the existing outer exception boundary record read failures. This repairs
the existing reporting contract without a new architecture or error policy.
The normal empty-input case remains valid. The regression and related diagnostics,
capture and actual complexity checks passed 47 tests; Ruff passed. The unreadable
input itself cannot be reconstructed by this change; reporting loss is not capture
success. Evidence: `logs/audit-2026-09-29-completed-repair-prompt-input-read-*.txt`.

### Storage fault and process-death qualification — 2026-09-29

The POSIX publication test injects ENOSPC at the staging-write boundary and EIO
at file and directory synchronization, separately for anchor, part and manifest.
Both the initial call and a repeat with surviving files must refuse acceptance.
After removing the injected fault, the real queue/coordinator publisher preserves
the full event and yields one task. This is error-path injection, not a power-loss
or physical disk-exhaustion experiment. The related batch passed 38 tests.

A separate subprocess test now calls `os._exit(73)` after receipt indexing,
permanent source publication, journal commitment, and terminal-file publication
before queue acknowledgement. It waits for actual persisted task/owner expiries
without editing clocks, lease rows or process identities. A fresh worker in each
temporary vault then verifies the full permanent source, one journal operation
and successful delivery; another pass has no work. Model calls fail the test.
The four independent lease waits overlap. An initial harness run failed before
the crash boundary because the subprocess lacked the scripts import path; that
failure is retained, not counted as successful crash qualification.

After correcting only the harness import setup, the combined storage, process
death, renewal shutdown, input diagnostics and actual complexity run passed
51 tests in 156.89 seconds. Test scheduling weights record this real duration.
Ruff passed. The latest live doctor still reports queue `ok`, overall `degraded`:
nightly completion is unverified and historical losses remain reported. Producer
activation, representation/retrieval/full-cycle cost, remaining audit findings,
final full-suite/security/platform checks and legacy cleanup remain open.
Evidence: `logs/audit-2026-09-29-completed-repair-breadcrumb-final-fault-qualification*.txt`.

### 2026-10-05: generic breadcrumbs and native identity

The transport accepts a canonical JSON object, while the host adapter publishes
an EventEnvelope without its event ID, content hash and two clocks. A generic
`{"prompt":"complete prompt"}` therefore remains supported physical evidence;
it does not gain native user-fact authority. The daily decoder previously applied
native schema validation to every linked captured JSON object. Three genuine
committed-journal/archive tests reproduced that refusal on the installed baseline.
Replacing their fixtures alone would hide this compatibility defect.

Complete JSON now selects native validation only through an outer
`schema_version`/`event_type` pair or the complete existing actor metadata plus
payload. The latter preserves refusal when both discriminators disappear from a
native record. An isolated agent, version or event-type field, or a nested pair,
does not declare a native format. Objects with the full native identity remain
ambiguous native-shaped input and fail closed if incomplete or unsupported.
Incomplete JSON retains the existing fragment recognizer separately. No schema,
physical source proof, canonical byte check or payload rule is relaxed. Native
multipart inputs still require a complete physical citation; generic multipart
inputs do not manufacture decoded user facts.

The alternatives were replacing generic fixtures, using string substrings as
format identity, and dispatching on parsed outer identity. The first loses
historical coverage; the second mistakes nested data and agent-only objects for
native events. The selected correction changes no transport, persisted schema,
path, dependency, database or environment contract. Historical ROOT generic-event
occurrence was not inventoried and is not asserted.

Primary sources were freshly read on 2026-10-05: [RFC 8259](https://www.rfc-editor.org/info/rfc8259/)
(object boundaries and duplicate-member interoperability), [Python 3.10.22 JSON documentation](https://docs.python.org/3.10/library/json.html)
(parsed dictionaries and explicit JSONDecodeError), and [JSON Schema conditional validation](https://json-schema.org/understanding-json-schema/reference/conditionals)
(property presence before variant validation; required properties remain required).
Three local decodes of one 1,080,203-byte EventEnvelope-derived frame took
0.02267 seconds. This measures parsing only; it does not prove a model-backed
compile cycle or installation. Original failures and interim fixture failures are
retained in private diagnostic logs.

Candidate qualification retained 14 original failing assertions and 17 controls.
The final eight related modules passed 170 tests in 40.58 seconds, including all
three unchanged generic archive scenarios and the same scenarios with real host
EventEnvelope input. Every one of the 15 changed/new callables was measured with
Lizard at its exact AST start line: maximum CCN 5, two if statements and two nested
branch/loop levels. Ruff passed. A new host-fixture error was corrected by passing
the actual selected batch inputs to apply; its strict production identity check
was not changed. No models were called and nothing was installed or committed.
