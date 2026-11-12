# Recovery rechecks the successor of a listed pending publication

Research date: 2026-10-02. The installed failure trail recorded a pending
manifest disappearing during capture adoption. A directory listing is not a
snapshot: another cooperating publisher can finish registration and remove the
pending manifest before the recovery visitor opens it. The ready manifest,
anchor, parts and replay-safe queue binding still exist. Treating the old path's
absence as lost evidence was a false finding.

Sources checked today: [Python scandir](https://docs.python.org/3/library/os.html#os.scandir)
documents unspecified inclusion of files changed during enumeration;
[SQLite isolation](https://www.sqlite.org/isolation.html) describes committed
visibility and transaction isolation; [PostgreSQL transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html)
illustrates why a later statement may see a different committed state. The latter
is a comparison, not a new dependency. Installed Python is 3.12.3 with SQLite
3.45.1; this correction uses APIs already supported by Python 3.10, and preserves
the operational rollback-journal/FULL contract.

The existing pending visitor first validates the listed path against its digest
identity and tries that exact file. Only FileNotFoundError triggers the existing
ready-or-pending reader for that identity. The normal format dispatcher still
verifies the complete manifest, anchor, linked parts and reconstructed input.
Registration still uses the canonical capture owner, intent fence and replay-safe
binding, so an already registered occurrence yields the same task. A missing or
damaged successor remains an explicit failure. Permission and containment errors
are not treated as completion. No schema, directory, configuration, service or
numeric limit changes.

Alternatives rejected: suppressing all disappearing paths without proof; a
sleep-and-retry loop that cannot prove completion; preserving every pending
manifest forever alongside its ready successor; locking the entire directory
scan and delaying unrelated publishers. The chosen reread reuses existing proof
and registration code and costs an additional bounded read only on this race.
Terminal purge and missing historical source recovery are separate questions;
this correction does not assert that absent ready evidence is successful work.

A real temporary SQLite queue/coordinator regression completes publication
between directory discovery and the old visitor's read at each of three prior
failure stages. The original implementation failed all three success scenarios;
three missing/damaged-evidence cases passed and are retained as negative controls.
The success assertions verify original bytes and one claimable task rather than
merely an empty error list. Red result: 3 failed / 3 passed. Final checks and
installation are recorded separately; this document is not full audit closure.

The live diagnostic also encountered an earlier publisher's retained staging
file, created at 11:57:10 UTC. The existing runtime publisher writes
`.destination.<32-hex-nonce>.tmp` before create-only publication. Inspection had
parsed the leading dot as an empty occurrence digest and declared the queue
unverifiable. It now recognizes only that exact existing staging contract,
then evaluates the occurrence through the same canonical manifest/anchor/part
proof. Staging alone stays incomplete; inspection does not publish or delete
it. Unknown dotfiles still fail. Two genuine pre-fix checks failed and the
unknown-file control passed after fixing two fixture setup mistakes; all
unsuccessful fixture logs are retained. This does not make a temporary file
an accepted event or waive incomplete-evidence findings.

A one-time operational recovery proved the retained part's canonical JSON,
identity, position, predecessor, hash, complete input length and input hash
against the immutable anchor. All 1096 input bytes were present. The existing
fenced publisher and replay-safe registration restored that exact occurrence
without a model call or invented content, in 0.122 seconds. The staging artifact
was retained for checked cleanup after installed proof. This is one recovered
occurrence, not complete historical recovery.

The first current full regression stopped at 1 failed / 5301 passed / 48 skipped
in 1054.98 seconds. Its nonwaiting-checkpoint mock did not accept the existing
writer_wait_seconds argument. The whole integration file reproduced five
failures and one error-path test was also exercising TypeError rather than its
intended RuntimeError. Six mocks now explicitly assert a zero wait, then run
the original callback. Original order, same-envelope, failure and single-event
assertions remain. Combined recovery/health/integration checks: 117 passed /
21 platform skips. This is not an assertion that the full suite is green.
