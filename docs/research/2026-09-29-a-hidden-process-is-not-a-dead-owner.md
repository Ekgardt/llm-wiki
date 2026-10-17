# A hidden process is not a dead owner

Current status, 2026-09-30: the qualified source changes described here are installed. See [the installation evidence](2026-09-30-durable-capture-installation.md) for the final regression, live capture and nightly results. The checkpoints below retain their original dates and describe the state at that checkpoint; their pending-installation statements are historical. Installation does not close the remaining warning review, every native-event qualification, or the wider limit audit.

Date: 2026-09-29. Status: reproduced defect; correction and safe rollout are open.

## Reproduction

Two ordinary managed shell calls have separate PID namespaces. A temporary
interactive Python process reports its real identity through the production
`operational_ownership.current_process_identity` function and waits for input.
A second shell calls production `process_identity_state` with that identity.
It reports `dead`. The original interactive process then accepts input, prints
its acknowledgement and exits normally. No operational owner or knowledge
record is created by this probe. The temporary identity/result files are under
`/tmp/wiki-live-process-probe.json` and `/tmp/wiki-cross-namespace-result.json`.

The owner's namespace was `pid:[4026532530]`, the observer's
`pid:[4026532591]`. The recorded PID was 2. The original process remained alive
through the observation. This is a real namespace reproduction, not just a
mocked missing PID. The native LLM Wiki graph was checked fresh before the
probe; connected ownership call sites were inspected against source because
the graph declares incomplete coverage.

## Cause and related consumers

The Linux start identity includes boot ID and start ticks, but no PID namespace.
`process_identity_state` treats a missing or different process at that numeric
PID as proof of death. The boolean `process_liveness.owner_alive` has the same
assumption. Numeric PIDs are only meaningful in the namespace that assigned them.
Related consumers include owner admission, Markdown preparers, compile/state
locks, queue fences, scheduled maintenance, LSP cleanup and language-server
installation. Fixing only doctor's wording would leave these consumers unsafe.

## Sources checked today

- [Linux PID namespace manual](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html):
  PID reuse across namespaces, visibility and the namespace of a procfs mount.
- [Bubblewrap upstream](https://github.com/containers/bubblewrap/blob/main/README.md):
  PID isolation hides processes outside the sandbox; its caller chooses isolation.
- [Kubernetes process namespace documentation](https://kubernetes.io/docs/tasks/configure-pod-container/share-process-namespace/):
  process visibility across containers requires explicitly shared process namespaces.

These are three independently maintained primary sources. The attempted kernel
resource-control documentation URL did not load and is not supporting evidence.
Observed runtime: Ubuntu 24.04, Linux 6.8.0-139-generic, Python 3.12.3,
distribution Bubblewrap 0.9.0-1ubuntu0.3. No platform was upgraded by this probe.

## Requirements for the correction

An ownership identity must bind the namespace in which its PID is meaningful.
An observer that cannot inspect that scope must answer `unknown`, preserving
the existing prohibition on reclaiming unknown owners. Same-scope exit and
PID-reuse detection must still work. Namespace scope must be checked before a
missing PID becomes evidence of death, not only after a successful procfs read.

Compatibility must be designed before deployment: old Linux identity strings
lack scope, and old readers would interpret a new identity format as a mismatch.
A format-only patch with mixed old/new clients is not safe. Recovery after reboot,
legacy identities, admission, cleanup, normal exit and interrupted rollout all
need explicit qualification. No implementation or migration is certified here.

Alternatives rejected: increasing expiry times does not restore visibility;
equating timeout with death repeats the error; guessing a host PID from local
numbers is unsafe; disabling sandboxing is not the product correction. Returning
unknown forever for all Linux processes would prevent necessary recovery, so
that alone is not a complete solution either.

## Current operational consequence

The audit's newly started nightly pass was stopped through its own execution
session with exit 130 before its post-compile maintenance stage. Before stopping,
it reported a failed snapshot, pruning artifacts of 1845 settled transactions,
zero dropped transaction/history rows and zero removed staged knowledge writes.
This records the program's output; it is not an independent proof that every
retention decision was safe. No new destructive cleanup is authorized by this
probe. The new compile also reported Codex read-only-home and Claude DNS failures.
The earlier successful manual compile is a different run.

Evidence: `logs/audit-2026-09-29-nightly-after-compile.*.log`,
`logs/maintenance/20260929T223313-reclaim-3.out.log`,
`logs/audit-2026-09-29-process-namespace-*.json`.
Private progress/log mutation is deferred while the ownership boundary itself
cannot be trusted across these execution contexts.

## Isolated correction under qualification

The candidate is developed only in `/tmp/llm-wiki-provider-verification-ewdevhsa`.
It keeps the existing identity field but versions the Linux value and binds the
boot, the observer PID namespace device/inode, and the process start ticks.
The procfs mount must describe the observer's namespace; a foreign procfs view
cannot be used to interpret `getpid()` numbers. A different namespace is unknown,
including when the local numeric PID does not exist. Same-scope exit and reuse
remain detectable. Persisted PID-only and old Linux identities are unknown,
because their missing scope cannot be reconstructed from today's process table.

For reboot recovery the candidate also includes an application-specific HMAC of
the optional system machine ID. A different boot proves death only with the same
available host key. A missing, unreadable or invalid machine ID is represented
explicitly as unavailable: same-boot operation still works, but automatic
cross-boot recovery cannot establish host continuity. No dependency on systemd
or new environment option is introduced. Cloned machine IDs violate the upstream
uniqueness requirement and cannot provide that continuity guarantee.

Additional primary sources checked on 2026-09-29:

- [Linux namespace handles](https://man7.org/linux/man-pages/man7/namespaces.7.html)
  document device/inode identity and handle access restrictions.
- [Linux procfs PID coordinates](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html)
  distinguish the mount's PID namespace from the reader's.
- [systemd machine-ID contract](https://github.com/systemd/systemd/blob/main/man/machine-id.xml)
  specifies the 32-hex-digit ID, host uniqueness, and an application-specific
  keyed hash instead of exposing the original ID. The hosted manual returned
  HTTP 403; its upstream source was read instead.

This choice avoids a privileged namespace-entering service, new runtime root,
or persistent daemon. The cost is conservative recovery when scope or host
continuity is absent. An expiry, namespace mismatch, or missing local PID never
overrides that uncertainty. Old readers are incompatible with the new format;
the candidate must not be copied into the running vault until a quiescent
cutover and recovery of existing records have been qualified. No such cutover
has occurred. This section records the experiment, not production completion.

### A second coordinate: the observer's time namespace

Reviewing the actual upstream Linux 6.8 implementation exposed a related case:
`do_task_stat` adds the reading process's time-namespace boot offset before
printing start ticks. Equal PID namespaces alone therefore do not make start
ticks comparable. The candidate must also record the observer's time namespace;
different clock coordinates mean unknown, not PID reuse. This finding comes
from source inspection, not a claimed live time-namespace reproduction.

Primary evidence:
[Linux 6.8 procfs stat implementation](https://raw.githubusercontent.com/torvalds/linux/v6.8/fs/proc/array.c),
[Linux 6.8 time namespace offset implementation](https://raw.githubusercontent.com/torvalds/linux/v6.8/include/linux/time_namespace.h),
[time namespace manual](https://man7.org/linux/man-pages/man7/time_namespaces.7.html).
The kernel's absent `CONFIG_TIME_NS` branch leaves the value unchanged; older or
feature-disabled kernels need an explicit absent-feature scope, not an invented
namespace number. An inaccessible existing handle remains unknown.

### Qualification so far

The initial PID-scoped candidate passed 103 targeted owner/lock tests, 35 recovery
tests, and the 11-test actual CCN/AST gate. The cross-command probe returned
`unknown`, kept the owner, and the original process then acknowledged input and
exited normally. These results precede the time-namespace extension above and
are not a final qualification of that extension.

A real mixed-reader probe confirmed that the installed old reader returns false
for `owner_alive` on a **live new-format owner**. The candidate keeps an old-format
owner as unknown. This is evidence that a mixed-client rollout is unsafe.
An interleaved warm-read microbenchmark of 1,000 probes per implementation measured
median 39.24 microseconds for the old reader and 154.63 for the candidate, with
p95 45.37 and 183.17 respectively. These are local probe costs, not full workflow
latency or product-wide token-efficiency evidence.

The broader Pyright test-file run was interrupted twice after hanging in
`HTTPResponse.begin`, before calling the downloader under test. A 15-second
diagnostic traceback and a separate socket-pair probe established that this
execution environment refuses `send` with `EPERM`; the test's server suppresses
that error and leaves its client waiting forever. No socket permissions were
bypassed and no test was weakened or reported as passed. Both broad runs and the
diagnostic run ended with exit 130. The test's error propagation needs correction
and the network-dependent qualification still needs a permitted environment.

## Later candidate checkpoint — still not installed

The candidate now includes the time namespace. The canonical ownership validator,
LSP writer and LSP doctor reader share the 512-character identity bound; the
installer's existing 256-byte field bound also fits the generated value and was
not widened. Doctor's separate
128-character LSP bound had rejected complete new identities; a failing test
exposed that mismatch before it was corrected.

The LSP review also found a separate unsafe inference: an expired or missing
lease was treated as a dead process, and failure retirement trusted `not live`.
The candidate probes recorded owner/manager/server identities even after expiry,
keeps any live process protected, reports unknown ownership as unreadable, and
requires positive death evidence for retirement. Deletion reuses the existing
guarded LSP removal path and checks death again. Unknown ownership no longer
creates an inferred crash record. Explicit historical failure evidence remains
evidence even when a current owner's state is unknown.

The HTTP fixture's error propagation is now corrected in the isolated copy:
headers are sent synchronously inside cleanup protection, unexpected body-send
errors are retained, and the peer closes on exit. A regression injects a refused
send and verifies both the reported cause and closed sockets. The real socket
test now fails promptly with the actual `PermissionError`, rather than hanging.
No networking rule was changed. The full installer test file completed with
107 passed, 1 failed for that permission refusal, and 9 platform skips.

Three healthy-vault fixtures and the real Pyright scope/deadline fixture also
inherited the execution environment's inaccessible `/tmp/.git` ancestor. Their
own temporary vaults now use real `git init` boundaries. The product's refusal
of an unreadable repository marker was retained; the ancestor was not removed
or changed. With the corrected fixtures, the full doctor/related-audit tests
plus namespace regression file passed 219 tests with 3 platform skips.

The combined related-file run completed with **445 passed, 1 failed, 12 skipped**
in 58.97 seconds. It includes the actual global Python/JavaScript CCN and Python
branch-shape checks. Its only failure is the real socket permission refusal.
Full Ruff over `scripts/` and `tests/` passed. The final new-namespace test fixture
subsequently received a platform-neutral `Path` comparison and the existing
`SHORT_TIMEOUT` test budget; its focused rerun and complexity gate passed **49 tests**
in 27.83 seconds and are recorded separately. No complete current-project regression suite or Windows/macOS run
is claimed by these results.

A second real two-command probe used the final PID/time-scoped format. The
observer in `pid:[4026532715]` saw the owner in `pid:[4026532654]` as `unknown`;
both the common lock reader and doctor kept it, LSP did not classify it as gone,
and doctor accepted the full identity. The original process then acknowledged
input and exited with code 0. This is a PID-namespace reproduction; differing
time namespaces remain covered by source analysis and regression fixtures.

The native refresh initially refused the isolated root because its coordinator
was still v2. Inspection found an empty coordinator, no transactions or owners,
and no queue. The normal queue constructor initialized the missing empty queue;
the supported offline v3 adoption then succeeded in this isolated root. This
does **not** qualify an upgrade with existing live or unknown owners. Production
operational state was not migrated or cleared.

The reviewable patch and source-hash manifest are evidence artifacts under
`logs/audit-2026-09-29-namespace-candidate-*`. Every existing production before-image
was checked against the original immutable native-index generation before
constructing the patch. They are not an instruction to install it immediately.

### Remaining release conditions

- Qualify a quiescent transition: no old in-memory reader may remain when new
  identities are written. This cannot be inferred from a PID list inside the
  isolated execution environment.
- Define and verify recovery of retained unscoped identities without relabelling
  unknown owners as dead or fabricating missing namespace information. Preserve
  source records and rollback evidence; expiry alone is not a migration proof.
- Run the socket-dependent check in a permitted environment, then the complete
  regression suite and relevant platform qualification.
- Only after a safe installed transition, reconcile the production health,
  private progress/log, documentation, and native index. The production index
  has not been refreshed for this research document while live ownership is unsafe.

The broader audit, breadcrumb-v2 activation, capture-host trust/qualification,
security-warning review, and legacy cleanup remain separate unfinished work.
Neither this candidate nor the audit is declared complete or compliant with all
nine laws at release level.


### Legacy writer crash-recovery correction — 2026-09-30

A fresh native graph (`generation-18da0f058ffb25bb-e337e6f4`, no changed sources)
and direct source inspection traced `_install_legacy_owner`, its claim caller,
`_writer_owner_reclaimable`, and `_add_writer_owner_columns`. The append test's
real subprocess exits at `after_applying`; recovery then failed in 11.04 seconds
waiting for the global writer gate. The legacy writer row lacked the identity
that the existing reader needs to distinguish a dead process from an unknown
PID. This is an implementation gap in the supported legacy path, not a faulty
assertion. Two additional regressions failed on the absent column.

The candidate uses the existing in-place legacy-column migration to add nullable
`process_start_identity`. New writer rows capture the actual current identity
before changing either ownership or fencing records. Existing rows stay NULL;
no process identity is inferred or backfilled. The pinned v3 table schema,
active paths, adoption protocol, lease durations and public interfaces are not
changed. Failure to obtain an identity refuses entry instead of creating an
unrecoverable new owner. This follows the already existing ownership reader and
legacy schema-expansion mechanisms; it introduces no second coordination store.

Alternatives considered: treating a missing local PID as death repeats the
namespace defect; extending the recovery timeout hides the missing evidence;
encoding identity into an unrelated token obscures its meaning; rewriting all
legacy databases into v3 on open bypasses the explicit adoption contract. The
nullable field preserves old rows and existing explicit INSERT column lists.
That syntactic compatibility does not make mixed old/new process readers safe:
the separately required quiescent installed transition still applies. This fix
does not certify the pre-existing legacy expiry-reclaim rule or authorize
retiring unqualified historical owners.

Primary sources checked on 2026-09-30:

- [SQLite ALTER TABLE](https://www.sqlite.org/lang_altertable.html): ADD COLUMN
  supports an additive nullable field without rewriting existing row contents;
  current documentation also describes newer operations that are not used here.
- [Linux PID namespaces](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html):
  PID visibility and procfs coordinates are namespace-relative, so absence in
  an observer's namespace does not establish recorded-owner death.
- [etcd API](https://etcd.io/docs/v3.6/learning/api/): lease expiry and ownership
  coordination are explicit protocol concepts. This is a comparison with a
  separate coordination model, not evidence that a local PID is dead or a
  recommendation to add etcd to this local product.

Validation runs use Python 3.12.3 and SQLite 3.45.1. The change uses long-standing
ADD COLUMN behavior and existing Python APIs compatible with the project's
Python 3.10 floor, with no dependency update. Initial crash-recovery and actual
repository-wide complexity/branch-shape checks passed 31 tests in 32.62 seconds;
49 related adoption, ownership, reused-PID and SQL identifier tests passed in
25.17 seconds. A final additional identity-probe-failure regression and updated
index results are recorded under `logs/audit-2026-09-30-legacy-writer-identity-*`.

No production implementation or operational database was changed. No alternate
writer implementation, new runtime path or compatibility switch remains in the
candidate. Legacy readers remain necessary for retained v2 data and explicit
adoption; removal requires verified migration of all consumers and retained
work, not just these passing tests. Private wiki/log updates remain deferred
until safe production ownership has been established. The overall audit,
historical-owner recovery and installed transition remain unfinished.


Final targeted qualification: 32 passed in 31.84 seconds, including refusal to
create any owner or fence when process identification fails. Wider transaction
qualification initially found five fixture setup failures: positional INSERTs
assumed exactly eight writer columns. All such fixtures using the actual legacy
coordinator now name their eight historical columns explicitly and leave the new
identity NULL. Two independent minimal-schema fixtures retain their positional
INSERTs because they define their own different schema. No assertion was removed
or weakened. The final transaction, migration and complexity group passed 210
tests with five platform skips in 56.01 seconds; Ruff passed on all five changed
files. The intermediate index refresh was interrupted after the follow-up test
edits, so it is not counted as completed evidence. Final refresh and freshness
results are recorded alongside the patch and source-hash manifest.
