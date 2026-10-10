# Joined evidence uses the existing canonical entry index

The installed partition correction left a second repeated parse in the same
closed-day packing path. The normal compiler ran for 1453.62 seconds before
dispatching a model call; process CPU was 1412.05 seconds. A 15-second profile
contained 1499 samples and no sampling errors. Entry-heading and marker scans
accounted for 1328 samples. This is a sampled stage observation, not the cost of
a completed compile.

`_evidence_unit_block` called `_declaring_entries`, which parsed the complete
immutable original day for every joined-unit quotation. It bypassed the active
`EvidenceResolver` and its existing `_immutable_byte_proof`. That proof already
retains the original bytes, canonical entries, digest, line index, and timestamp
groups. The compiler now asks the exact canonical resolver type for those
groups. The resolver's new method exposes its existing proof; it adds no cache
or lifetime. Outside the context, or with a foreign object, full parsing remains.

The common entry lookup also serves ordinary `_entry_block` calls. Membership,
partition identity, source hashes, whole-line quotation, ambiguity, DLP,
protected reads, receipts and transaction preconditions retain their checks.
Changed immutable bytes require another proof. No caller-provided entry metadata
is used by this method. The resolver is operation-local; this does not cache
permission to publish or approve an operational database.

## Qualification

The isolated previous code gives three causal failures and ten passes. The
correction passes fourteen focused tests on Python 3.10. The linked compile,
native and resolver regression passes 717 tests; the subsequently added changed-
bytes case also passes in the focused run. The changed test file's shard weight
is measured by the unchanged planner. Actual AST/Lizard analysis gives maximum
CCN 4 for the changed and new callables, with the required branch and nesting
bounds. Static analysis passes.

Two boundary lookups on the same current 11,103,934-byte original take 0.280
seconds and two full scans before the correction, versus 0.000026 seconds and
no full scans with the already-warmed existing proof. The complete returned
boundaries match, with result SHA-256
`a067b302781ae361871bfd1010844341043d88645bb59b4ba758236527686ff9`.
Source SHA-256:
`355eec01a474832a257a8fb791839a875844c2ed8aa547657a9da6fc3021e244`.
The source digest was checked again after measurement. This is not full-cycle,
token, retrieval, or product-completion evidence. An earlier probe expected the
previous run's source size and stopped at its source assertion before measuring;
it is not treated as a successful pair.

The first causal run on the installed live vault also failed the untouched
no-vault-writes teardown guard when session capture files appeared during the
tests. The same causal tests were repeated in the isolated checkout. Capture
files and the guard were preserved.

## Research and alternatives — 2026-10-10

The supported floor remains Python 3.10; no dependency versions change.

- [PSF Context Variables](https://docs.python.org/3.10/library/contextvars.html)
  describes context-local state and restoration. Reuse follows the existing
  resolver context rather than introducing global state.
- [SQLite isolation](https://www.sqlite.org/isolation.html) distinguishes
  committed operational state from process-local derived metadata. Canonical
  byte parsing does not replace database or transaction approval.
- [OWASP Input Validation](https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html)
  supports retaining semantic validation of actual input. The resolver derives
  boundaries from immutable bytes; it does not trust supplied metadata.

Fresh full parsing for every quote repeats the measured CPU cost. Trusting
`DailySnapshot.original_entries` directly could accept forged metadata. Another
global cache would add an unnecessary lifetime and invalidation contract.
Increasing a deadline would preserve the repeated scans. Exposing the existing
canonical byte proof removes the bypass while retaining independent final reads
and validation. The full parser remains necessary without an owned resolver;
it is an authority path, not obsolete code.
