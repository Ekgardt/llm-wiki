# Receipt authority must respect its caller deadline

Research date: 2026-10-03. Candidate only; no installation claimed.

Baseline before this repair: the approved v4 selector discovers receipts once and checks committed transaction authority for every positive result. `MarkdownCoordinator.committed_attempt` opened several connections through `_record_for_operation_id`, `_committed_attempt_by_ordinal`, `_record`, and `_record_if_present`. Those opens used the default busy timeout and had no caller deadline. An outer check could not bound a blocked inner connection.

Primary sources checked before this design:

- [SQLite busy timeout](https://www.sqlite.org/c3ref/busy_timeout.html): lock waiting is connection-specific; the accumulated wait budget ends with SQLITE_BUSY. It does not bound statement computation.
- [Python 3.10 sqlite3](https://docs.python.org/3.10/library/sqlite3.html#sqlite3.Connection.set_progress_handler): connection timeout bounds lock waiting; a progress handler can interrupt running statements. A nonzero callback result produces OperationalError.
- [Linux clock_gettime](https://man7.org/linux/man-pages/man2/clock_gettime.2.html): monotonic time is distinct from wall-clock updates. Existing caller deadlines use Python monotonic time.

Alternatives: outer checks alone leave blocking reads unbounded; a thread timeout leaves a live SQLite reader behind; a second independent receipt authority reader duplicates transaction integrity logic; optional deadline propagation preserves the canonical reader and old callers.

Selected design: optional deadline on the existing authority-read call chain. Preserve calls without a deadline. Derive each connection busy timeout from the smaller existing timeout and remaining caller budget, never introduce a new fixed allowance. Check expiry before/after opens and between queries; install a connection-local SQLite progress callback for finite deadline reads. Translate deadline interruption into visible TimeoutError; other corruption and SQLite failures remain failures. No persisted schema, database, environment contract, runtime path, daemon, or global writer deadline changes.

Qualification required: genuine expired-call refusal, actual lock contention under a short caller deadline, query-progress interruption, unchanged no-deadline behavior, committed receipt byte/integrity checks, actual CCN/AST shape for every changed callable, and full selector cost. Generic OS stalls cannot be claimed hard real-time bounded. No paid-model calls are needed for these checks.

## Current compatible candidate and measured cost

The candidate carries the same optional caller clock through cold adoption admission, its validation mutex and retry, database opening, canonical receipt reads, source-failure recording/reading/clearing, and diagnostic mirror lock acquisition. Existing waits are capped by remaining caller time. Infinite/default callers retain their previous behavior. There is no new resource budget, persisted format beyond the separately approved v4 context receipt, environment contract, runtime path, or ownership bypass.

Archive constructors now pass that clock to the existing canonical factories. Legacy receipt fallback, committed-operation and retention reads, commit-sequence reads, and writer-state reads use the same canonical authority connection and clock. An expired clock is a visible refusal; a database operational failure is not classified as receipt corruption. Malformed receipt content retains its fail-closed diagnostics. The former six-branch operation-state function is split into JSON identity and canonical record-state readers; the actual Lizard/AST checks cover both helpers.

The [SQLite progress-handler contract](https://www.sqlite.org/c3ref/progress_handler.html) specifies periodic VM-instruction callbacks and cooperative interruption. The initial one-instruction instrumentation cost about 0.384 seconds for a controlled recursive 100,000-row SUM, compared with about 0.019 seconds without a callback. Measured intervals 10, 100, 1,000, and 10,000 were also compared. The selected 1,000-instruction interval measured about 0.0194 seconds; a 20 ms interrupted workload overshot by less than 16 microseconds in that experiment. This is measured callback granularity, not an admission limit borrowed from a row batch. Scheduling, filesystem I/O, SQLite calls into a Python function, and operating-system stalls prevent any hard real-time promise. Requalify the tradeoff when the workload or SQLite implementation changes.

Final isolated cold admission → canonical v4 authority → source-failure record/read/clear cycle measured 0.03650 seconds without a finite clock and 0.04196 with it. Warm measurements were 0.03008 and 0.03669 seconds. Both produced the same authoritative source and settled failure; preparation proved the published page and exact physical quote. No real model was called. The frozen retained sample contains 222 historical v3 receipts, not a fabricated subset of the earlier 208-count inventory: all 222 qualified through unchanged strict historical authority in both modes, taking 0.81796 and 0.83595 seconds. Order and filesystem caches were not controlled, so these are observed costs and parity evidence, not a speedup claim. A preceding locked measurement failed visibly and remains a failed attempt. The original 2.640387-second interval-one measurement is historical baseline evidence, not the final price.

The combined consumer suite before the additional archive-clock repair passed 609 tests with three skips in 73.96 seconds. A broader operational suite passed 501 with eight skips and one stale test-observer failure: its wrapper did not forward the new optional deadline keyword. The wrapper was corrected without changing the disappearing-row assertion; that focused regression passed. Neither result is presented as a successful final combined run after this archive repair. The subsequent archive qualification includes eight original expired-clock assertion failures, a real locked-database refusal/recovery guard, and the unchanged archive assertions. The final combined candidate qualification then passed 1136 tests with 11 platform skips in 233.39 seconds; its evidence is `logs/audit-2026-10-03-step4-final-all-related-regressions.log` in the installed private vault.

On 2026-10-04 the approved source-context implementation was installed under the existing compile exclusion and canonical Markdown writer gate. A fresh installed stdio MCP process completed initialization and exposed all 12 tools. The exact installed source copy passed 162 focused regressions in 29.16 seconds before removal of three verified unused v3 producer helpers. Strict historical v3 readers and schemas remain necessary for retained receipts and archives. This installation checkpoint does not establish a native model cycle, the complete corrected nightly, or closure of the remaining resource-limit audit.

Private, source-hash-bound reports retain exact environment, workload, measurements, refusals, and qualification: `step4-progress-granularity-experiment`, `step4-cold-authority-full-cycle-all-failure-writes-cost`, `step4-historical-strict-authority-final-dispatch-lock-visible-cost`, and the `step4-archive-clocks-original-reds` log. No private source content is published by this document.

Provider probing and model transport still use their independent per-call timeout contracts. This repair does not make the whole compiler respect one end-to-end clock; provider-clock propagation remains a separately recorded later task. Candidate qualification does not claim installation, native-event completeness, or original audit closure.

Nor is this a whole-archiver deadline guarantee: outer writer-gate acquisition, queue source-reference and single-failure retention lookups, source fences/heartbeats, recovery, evidence traversal, and filesystem publication retain their separate existing contracts. Their exact callers remain investigation/remediation entries. The qualified scope here is archive receipt authority and canonical admission, not a redesign of retention or writer/finalization ownership.


### 2026-10-06: graph seed membership does not multiply evidence

The retrieval neighbor query joined each source occurrence to an assertion.
Multiple occurrences of the same seed node repeated identical assertion/evidence
rows although the returned row contains no seed-occurrence identity. A genuine
writer-built graph control with two occurrences returned duplicate evidence;
forty occurrences exceeded the existing row ceiling. The query now uses an IN
subquery for seed-node membership, retaining every distinct assertion/evidence
row, direction, edge filter, path, canonical target occurrence, ordering and
existing deadline/row guards. It introduces no index or schema change.

The original corrected fixture produced two failures and three passing controls;
201 related tests pass after the change. Earlier fixture refusals for unsupported
unresolved assertions and an incorrect expected incoming order were preserved
and corrected before the genuine baseline test. No existing assertions changed.

SQLite 3.45.1 EXPLAIN on the real active graph places the original seed occurrence
join after evidence joins; the candidate has a LIST SUBQUERY instead. The sampled
Oct2 daily path has no occurrences in that active graph, so this observation is
not proof of the running compile's seed or its wall-time improvement. No expensive
real neighbor query or model call was run. DISTINCT on final rows was rejected:
it would still construct the multiplied intermediate rows.

Primary sources read 2026-10-06:
[SQLite IN semantics](https://www.sqlite.org/lang_expr.html),
[PostgreSQL 18 subquery semantics](https://www.postgresql.org/docs/current/functions-subquery.html),
and [Python 3.10 SQLite deadlines](https://docs.python.org/3.10/library/sqlite3.html).
PostgreSQL is supporting relational-semantics evidence, not the deployed engine.


### 2026-10-06: reader-owned canonical breadcrumb parsing

Repeated current-source restoration validates the same canonical breadcrumb
records. A disposable reader-owned context retains exact immutable record bytes
paired with freshly read schema bytes. It reuses only schema/canonical-byte
validation, returns fresh JSON objects, and refuses schema changes during that
reader lifetime. Source, head, part, chain, path, deadline and receipt checks still
run. Finally resets the context; standalone reads remain uncached.

Original control: 28 validations for three distinct record/schema pairs. Candidate:
138 related tests pass, including changed records, schema drift, object mutation,
external head/part tampering and expiry. Two real captures of one unchanged daily
source reduced validation calls 12,880 to 1,380, retaining all 2,760 external
document reads and 22,748 frame checks. Wall time: 3.529 versus 2.819 seconds;
peak RSS: 126,504 versus 129,400 KiB. Shared-machine observations do not prove
whole-health completion or isolated latency improvement.

Existing record-size guards remain. Memory grows with distinct records in one
reader and is released with it. Whole-frame or authority-verdict caching was
rejected: SQL snapshots do not stabilize filesystem evidence. Primary sources
read 2026-10-06: [Python 3.10 immutable bytes](https://docs.python.org/3.10/library/stdtypes.html#bytes-objects),
[Python context ownership](https://docs.python.org/3.10/library/contextvars.html),
[SQLite isolation](https://www.sqlite.org/isolation.html), and
[Git racy-file identity](https://git-scm.com/docs/racy-git).

The subsequent actual transaction-only diagnostic retained its 60-second deadline
and refused after 38.192 seconds: transaction snapshot changed during filesystem
inspection. It reported 101 unresolved quarantines and read_error, with no invalid
state proof. Peak RSS was 973,060 KiB. That diagnostic includes SQL fingerprint
instrumentation and concurrent activity; it does not prove whole-health success
or compare performance on matched SQL inputs. The historical authority refusal
remains visible.
