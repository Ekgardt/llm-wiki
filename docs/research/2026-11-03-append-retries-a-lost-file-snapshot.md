# An append retries a lost file snapshot

2026-11-03. Reproduced defect corrected locally; full regression/native confirmation pending.

## Failure and cause

Native CI41720238934 atc2dd7466, Linux Python3.14 shard2, failed
`test_concurrent_identical_append_converges_once_during_distinct_event_churn`.
A file changed while `_stage_append` hashed its live VerifiedByteRange. The owner
check correctly refused the changed snapshot, but `_require_unchanged_read`
raised plain ValueError. Append retries PreconditionChangedError; it therefore
propagated this ordinary optimistic race as a permanent caller failure.
The same classification gap exists while initially capturing the append target,
before `_prepare_append_candidate` handles errors. SQLite transaction isolation
does not make a separately read Markdown file immutable.

The original concurrent test passed once locally; that does not refute the race.
The deterministic regression mutates the real temporary file at the two exact
boundaries and checks that both the winner and this append survive exactly once.
Before the fix: six failures, one pass. The passing control proves an unrelated
invalid-input ValueError is not retried. Direct checks cover in-place change and
path replacement for both read and hash snapshot validation. Evidence is retained
under logs/append-capture-race-red*.txt and ci-c2dd7466-linux-s2-failure.txt.

## Correction

Use the existing ValueError subclass PreconditionChangedError at the nine places
that explicitly detect a changed target read/hash snapshot. Keep every metadata,
identity, size, parent, containment and digest check, and every diagnostic string.
At the outer append-attempt boundary also recognize this type, so initial capture
loss re-enters the same complete read/prepare/CAS operation. Do not retry arbitrary
ValueError, unsafe opens, symlinks, size violations or unrelated failures.

The existing deadline, cancellation and stall guards still bound retries. No new
sleep, attempt limit, timeout increase, lock relaxation, schema or runtime root.
Failed preparation cleanup remains unchanged. Ordinary prepare callers still get
a ValueError subtype and must decide how to handle it; no partial success is
reported. The fix is separate from ledger decomposition and does not change the
public append promise to exactly-once delivery.

## Research and alternatives

Checked2026-11-03, three independent primary publishers:
- [SQLite isolation](https://www.sqlite.org/isolation.html): serialization applies
  to database operations; it is not a guarantee about separately accessed files.
- [Python OS interfaces](https://docs.python.org/3.14/library/os.html#os.replace):
  path replacement and descriptor metadata are different observations.
- [PostgreSQL serialization failure handling](https://www.postgresql.org/docs/current/mvcc-serialization-failure-handling.html):
  classify retryable conflicts and repeat the whole transaction, including the
  logic that chooses values. This is an analogous concurrency principle, not a
  PostgreSQL dependency or claim that its isolation applies to this product.

Weakening stable-read checks risks mixed images. Retrying every ValueError hides
invalid requests. Catching a diagnostic substring couples correctness to wording.
A typed existing precondition loss preserves fail-closed checks and the already
implemented full-attempt retry protocol.

Fresh built-in indexaab398c7 and logs/append-capture-race-graph.json preceded new
code; full relevant caller, capture, staging, cleanup and retry source was read.

## Local qualification

The six previously failing scenarios now pass. With the cancellation guard,
331related tests pass and5platform cases skip on Linux;690global module/structure
checks pass. Full collection succeeds with17950cases. ActualCCN/shape and Ruff pass.
All134maintenance packets reconstruct exactly and remain <=100000tokens.
Evidence: logs/append-capture-race-{related,global,bundles,collection}.*.
The original concurrent stress case is included in the related run. This is not
native Windows/macOS or full-suite confirmation; both remain required.
